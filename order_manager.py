"""
order_manager.py — place orders and confirm they actually fill.

Why this exists
---------------
Before this module, every strategy placed an order and immediately recorded
the position at the *mid price*, assuming the fill happened at the price we
asked for. That is wrong in three ways:

  1. A limit order may not fill at all (or only partially).
  2. A market order fills at the actual NBBO, not the mid we computed.
  3. A rejected order would still get recorded as an open position.

`place_and_confirm()` places the order, then polls the broker until the order
reaches a terminal state (filled / partially_filled / rejected / canceled /
expired) or a timeout elapses. It returns the REAL fill price and the REAL
filled quantity, so position records reflect what actually happened.

Idempotency
-----------
Every order is placed with a unique client `tag`. The tag is recorded in a
local pending-orders ledger (pending_orders.json) BEFORE the network call.
If the bot crashes between submit and confirmation, `recover_pending_orders()`
on the next startup looks each tag up at the broker and resolves it, so we
never lose track of a fill or blindly re-submit.

Public API
----------
    place_and_confirm(...) -> FillResult
    recover_pending_orders() -> list[FillResult]
    make_order_tag(strategy, symbol) -> str

FillResult fields
-----------------
    ok              bool   — order reached a usable terminal state with fills
    status          str    — final broker status
    filled_qty      int    — contracts actually filled
    avg_fill_price  float  — average fill price per share (×100 = per contract)
    order_id        str
    tag             str
    reason          str    — human-readable summary / error
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

from logger import get_logger
from state_io import file_lock, atomic_write_json, read_json
from tradier_client import (
    place_option_order, get_order_status, find_orders_by_tag, cancel_order, MOCK_MODE,
)

log = get_logger("order_manager")

PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_orders.json")

# Order lifecycle
_TERMINAL_FILLED   = {"filled"}
_TERMINAL_PARTIAL  = {"partially_filled"}
_TERMINAL_DEAD     = {"rejected", "canceled", "cancelled", "expired", "error"}
_WORKING           = {"open", "pending", "submitted", "accepted", "calculated", "queued"}

# Poll config
POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class FillResult:
    ok: bool
    status: str
    filled_qty: int
    avg_fill_price: float
    order_id: str
    tag: str
    reason: str

    @property
    def partially_filled(self) -> bool:
        return self.status in _TERMINAL_PARTIAL


# ─── Pending-order ledger (atomic) ────────────────────────────────────────────

def _load_pending() -> dict:
    data = read_json(PENDING_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_pending(pending: dict) -> None:
    try:
        atomic_write_json(PENDING_FILE, pending)
    except Exception as e:
        log.warning("Could not persist pending orders: %s", e)


def _record_pending(tag: str, meta: dict) -> None:
    # Locked RMW — all three strategy processes share this ledger.
    with file_lock(PENDING_FILE):
        pending = _load_pending()
        pending[tag] = {**meta, "recorded_at": time.time()}
        _save_pending(pending)


def _clear_pending(tag: str) -> None:
    with file_lock(PENDING_FILE):
        pending = _load_pending()
        if tag in pending:
            del pending[tag]
            _save_pending(pending)


def make_order_tag(strategy: str, symbol: str) -> str:
    """Build a unique, broker-safe client tag for an order."""
    short = "".join(c for c in f"{strategy}{symbol}" if c.isalnum())[:24]
    return f"{short}-{uuid.uuid4().hex[:8]}"


# ─── Core: place + confirm ────────────────────────────────────────────────────

def _interpret(order: dict, tag: str, order_id: str) -> FillResult | None:
    """
    Turn a broker order dict into a FillResult if it's terminal, else None
    (meaning: keep polling).
    """
    status = str(order.get("status", "")).lower()

    filled_qty = int(float(order.get("exec_quantity", 0) or 0))
    avg_price  = float(order.get("avg_fill_price", 0) or 0)

    if status in _TERMINAL_FILLED:
        return FillResult(True, status, filled_qty, avg_price, order_id, tag,
                          f"filled {filled_qty} @ ${avg_price:.2f}")

    if status in _TERMINAL_PARTIAL:
        # Partial is terminal only if the order is no longer working. Tradier
        # reports partially_filled while still open; treat as non-terminal
        # unless remaining_quantity is 0.
        remaining = float(order.get("remaining_quantity", 0) or 0)
        if remaining <= 0:
            return FillResult(filled_qty > 0, status, filled_qty, avg_price,
                              order_id, tag, f"partial fill {filled_qty} @ ${avg_price:.2f}")
        return None  # still working

    if status in _TERMINAL_DEAD:
        # Canceled/expired/rejected. If some contracts filled before the order
        # died (partial fill that then got canceled), those are genuinely ours
        # — surface them as a fill so the caller records the position rather
        # than ignoring a real fill.
        if filled_qty > 0:
            return FillResult(True, "partially_filled", filled_qty, avg_price, order_id, tag,
                              f"{status} after partial fill of {filled_qty} @ ${avg_price:.2f}")
        return FillResult(False, status, 0, avg_price, order_id, tag,
                          f"order {status}: {order.get('reason_description', 'no reason given')}")

    # Working states → keep polling
    return None


def place_and_confirm(
    symbol: str,
    option_symbol: str,
    side: str,
    quantity: int,
    order_type: str = "market",
    price: float | None = None,
    duration: str = "day",
    strategy: str = "unknown",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fallback_price: float | None = None,
) -> FillResult:
    """
    Place an order and poll until it reaches a terminal state or `timeout`.

    `fallback_price` is used as avg_fill_price only if the broker reports a
    fill but returns no price (rare, but Tradier can omit avg_fill_price on
    fast market fills) — keeps downstream P&L math from dividing by zero.

    In MOCK_MODE returns a simulated successful fill at `price`/`fallback_price`
    so the rest of the pipeline can be exercised without a broker.
    """
    tag = make_order_tag(strategy, symbol)

    if MOCK_MODE:
        px = price or fallback_price or 0.0
        log.info("[MOCK] simulated fill %s %dx %s @ $%.2f", side, quantity, option_symbol, px)
        return FillResult(True, "filled", quantity, px, "MOCK", tag, "mock fill")

    # Record intent BEFORE the network call so a crash is recoverable.
    _record_pending(tag, {
        "symbol": symbol, "option_symbol": option_symbol, "side": side,
        "quantity": quantity, "strategy": strategy, "order_type": order_type,
    })

    order = place_option_order(
        symbol=symbol, option_symbol=option_symbol, side=side,
        quantity=quantity, order_type=order_type, price=price,
        duration=duration, tag=tag,
    )

    if order.get("status") == "error":
        _clear_pending(tag)
        return FillResult(False, "error", 0, 0.0, "", tag,
                          f"submit failed: {order.get('error')}")

    order_id = str(order.get("id", ""))

    # Poll loop
    deadline = time.time() + timeout
    last_status = str(order.get("status", "pending"))
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        cur = get_order_status(order_id)
        if cur.get("status") == "error":
            continue  # transient — keep trying until deadline
        last_status = str(cur.get("status", last_status))
        result = _interpret(cur, tag, order_id)
        if result is not None:
            if result.ok and result.avg_fill_price <= 0 and fallback_price:
                result.avg_fill_price = fallback_price
                result.reason += f" (price defaulted to ${fallback_price:.2f})"
            _clear_pending(tag)
            log.info("Order %s resolved: %s", order_id, result.reason)
            return result

    # Timed out while still working. Cancel the unfilled order so it can't fill
    # minutes later as an unmanaged surprise position, then resolve its TRUE
    # final state — the cancel may have raced a fill, or it may have partially
    # filled before we cancelled.
    log.warning("Order %s still '%s' after %.0fs — cancelling unfilled remainder",
                order_id, last_status, timeout)
    try:
        cancel_order(order_id)
    except Exception as e:
        log.warning("Could not cancel timed-out order %s: %s", order_id, e)

    final = get_order_status(order_id)
    if final.get("status") != "error":
        result = _interpret(final, tag, order_id)
        if result is not None:
            # Definitive terminal state (filled / partial / dead-no-fill).
            if result.ok and result.avg_fill_price <= 0 and fallback_price:
                result.avg_fill_price = fallback_price
            _clear_pending(tag)
            if result.ok and result.filled_qty > 0:
                log.info("Order %s resolved after cancel: %s", order_id, result.reason)
            return result

    # Couldn't confirm the outcome (status fetch failed). Leave it in the ledger
    # so startup recovery reconciles it; do NOT assume a fill.
    log.warning("Order %s outcome unresolved after timeout — left in pending ledger for recovery", order_id)
    return FillResult(False, last_status or "timeout", 0, 0.0, order_id, tag,
                      f"timed out after {timeout:.0f}s; outcome unresolved")


# ─── Crash recovery ───────────────────────────────────────────────────────────

def recover_pending_orders() -> list[FillResult]:
    """
    Called at startup. For each tag in the pending ledger, look it up at the
    broker and resolve its true outcome. Clears resolved entries.

    Returns the list of resolved FillResults so the caller can re-journal any
    fills that completed while the bot was down.
    """
    pending = _load_pending()
    if not pending:
        return []

    log.info("Recovering %d pending order(s) from previous session", len(pending))
    resolved: list[FillResult] = []

    for tag, meta in list(pending.items()):
        orders = find_orders_by_tag(tag)
        if not orders:
            # Never reached the broker — safe to drop.
            log.info("Pending tag %s not found at broker — order never placed, clearing", tag)
            _clear_pending(tag)
            continue

        order = orders[0]
        order_id = str(order.get("id", ""))
        result = _interpret(order, tag, order_id)
        if result is None:
            # Still working — leave it; we'll catch it next startup or it'll
            # expire. Don't clear.
            log.info("Pending tag %s still working at broker (status=%s)", tag, order.get("status"))
            continue

        if result.ok and result.filled_qty > 0:
            # A fill completed while we were down. The position may not be in
            # the local book (so not exit-managed). Warn loudly and notify the
            # operator to verify it, rather than silently dropping it.
            sym = meta.get("option_symbol", "?")
            log.warning("RECOVERED FILL while bot was down: %s %sx %s (%s) @ $%.2f — "
                        "verify it is tracked / exit-managed",
                        meta.get("side"), result.filled_qty, sym, meta.get("strategy"),
                        result.avg_fill_price)
            try:
                from event_notifier import _dispatch
                _dispatch(
                    subject="Order filled while bot was offline",
                    lines=[f"{meta.get('side')} {result.filled_qty}x {sym} ({meta.get('strategy')})",
                           f"Fill price: ${result.avg_fill_price:.2f}",
                           "This filled during downtime — confirm it's tracked and has exit management."],
                    severity="warning",
                )
            except Exception:
                pass
        else:
            log.info("Recovered tag %s: %s", tag, result.reason)
        resolved.append(result)
        _clear_pending(tag)

    return resolved


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Order manager utilities.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("pending", help="Show the pending-orders ledger")
    sub.add_parser("recover", help="Resolve pending orders against the broker")
    args = parser.parse_args()

    if args.cmd == "pending":
        p = _load_pending()
        if not p:
            print("No pending orders.")
        else:
            for tag, meta in p.items():
                print(f"  {tag}: {meta.get('side')} {meta.get('quantity')}x "
                      f"{meta.get('option_symbol')} ({meta.get('strategy')})")
    elif args.cmd == "recover":
        results = recover_pending_orders()
        if not results:
            print("Nothing to recover.")
        for r in results:
            print(f"  {r.tag}: {r.status} — {r.reason}")
    else:
        parser.print_help()
