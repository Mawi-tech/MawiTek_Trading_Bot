"""
kill_switch.py — emergency "flatten everything and stop".

One command that, in order:

    1. Cancels every working order at the broker (so nothing new fills).
    2. Closes every open option position at market (longs sell_to_close,
       shorts buy_to_close), using the BROKER as the source of truth so it
       catches positions the local files don't know about.
    3. Sets the halt flag in risk_state.json so none of the running strategy
       processes will open new trades — they all honour the same flag via
       risk_manager.pre_trade_check().

Use it when something is clearly wrong and you want the bot OUT of the market
NOW, without hunting through three terminals.

Usage:
    python kill_switch.py             # asks for confirmation first
    python kill_switch.py --force     # skip the prompt (for scripts/hotkeys)
    python kill_switch.py --status    # show what WOULD be flattened, do nothing

Programmatic:
    from mawitek.core.kill_switch import flatten_all
    summary = flatten_all(reason="manual panic")

NOTE: setting the halt flag stops NEW trades but does not kill the running
processes. To also stop the loops, Ctrl+C start_all.py (or stop the services).
The halt flag alone is enough to make them idle safely.
"""

from __future__ import annotations

import argparse
import datetime

from mawitek.data.tradier_client import (
    get_open_positions, get_open_orders, cancel_order, MOCK_MODE,
)
from mawitek.core.order_manager import place_and_confirm
from mawitek.infra.logger import get_logger
from mawitek.infra.state_io import file_lock
from mawitek.infra.utils import today_est

log = get_logger("kill_switch")

RISK_STATE_FILE = "risk_state.json"


def _occ_underlying(symbol: str) -> str:
    """Pull the underlying ticker off the front of an OCC option symbol."""
    out = []
    for ch in symbol:
        if ch.isdigit():
            break
        out.append(ch)
    return "".join(out) or symbol


def cancel_all_orders() -> int:
    """Cancel every working order. Returns the count cancelled."""
    orders = get_open_orders()
    n = 0
    for o in orders:
        oid = o.get("id")
        if oid is None:
            continue
        if cancel_order(str(oid)):
            n += 1
            log.info("Cancelled order %s (%s %s)", oid, o.get("side"), o.get("option_symbol", o.get("symbol")))
    return n


def close_all_positions() -> dict:
    """
    Market-close every open option position at the broker.

    Returns {"closed": n, "failed": n, "details": [...]}.
    """
    positions = get_open_positions()
    closed, failed, details = 0, 0, []

    for pos in positions:
        symbol = pos.get("symbol", "")
        qty    = float(pos.get("quantity", 0) or 0)

        # Only options (OCC symbols are long); skip equities and zero-qty rows.
        if qty == 0 or len(symbol) <= 6:
            continue

        underlying = _occ_underlying(symbol)
        side = "sell_to_close" if qty > 0 else "buy_to_close"

        log.info("Flattening %s x%d via %s", symbol, abs(int(qty)), side)
        fill = place_and_confirm(
            symbol=underlying,
            option_symbol=symbol,
            side=side,
            quantity=abs(int(qty)),
            order_type="market",
            strategy="kill_switch",
            timeout=20.0,
        )
        if fill.ok and fill.filled_qty > 0:
            closed += 1
            details.append(f"{symbol}: closed {fill.filled_qty} @ ${fill.avg_fill_price:.2f}")
        else:
            failed += 1
            details.append(f"{symbol}: CLOSE FAILED — {fill.reason}")
            log.error("Failed to flatten %s: %s", symbol, fill.reason)

    return {"closed": closed, "failed": failed, "details": details}


def set_halt(reason: str = "kill_switch") -> None:
    """Force the daily halt flag on so no strategy opens new trades."""
    # Local import to avoid importing risk_manager's broker calls unless needed.
    from mawitek.core.risk_manager import load_state, save_state
    with file_lock(RISK_STATE_FILE):
        state = load_state()
        state["halted"] = True
        state["halt_reason"] = reason
        state["halt_time"] = datetime.datetime.now().isoformat(timespec="seconds")
        state["date"] = today_est().isoformat()
        save_state(state)
    log.info("Halt flag set (reason: %s)", reason)


def flatten_all(reason: str = "manual", set_halt_flag: bool = True) -> dict:
    """
    The full kill sequence. Returns a summary dict.
    """
    log.warning("=== KILL SWITCH ENGAGED (%s) ===", reason)

    if MOCK_MODE:
        log.warning("MOCK_MODE — no live broker. Nothing to flatten; setting halt flag only.")
        if set_halt_flag:
            set_halt(reason)
        return {"mock": True, "orders_cancelled": 0, "positions": {"closed": 0, "failed": 0, "details": []}, "halted": set_halt_flag}

    cancelled = cancel_all_orders()
    log.info("Cancelled %d working order(s)", cancelled)

    result = close_all_positions()
    log.info("Closed %d position(s), %d failed", result["closed"], result["failed"])

    if set_halt_flag:
        set_halt(reason)

    # Notify if a channel is configured.
    try:
        from mawitek.infra.event_notifier import _dispatch
        _dispatch(
            subject="KILL SWITCH engaged — flattened",
            lines=[
                f"Reason: {reason}",
                f"Orders cancelled: {cancelled}",
                f"Positions closed: {result['closed']} (failed: {result['failed']})",
                "Halt flag set — no new trades until cleared." if set_halt_flag else "Halt NOT set.",
            ],
            severity="danger",
        )
    except Exception as e:
        log.warning("kill-switch notification failed: %s", e)

    return {
        "orders_cancelled": cancelled,
        "positions": result,
        "halted": set_halt_flag,
    }


def show_status() -> None:
    """Dry-run: print what would be flattened."""
    if MOCK_MODE:
        print("MOCK_MODE — no broker connected. Nothing to show.")
        return
    orders = get_open_orders()
    positions = [p for p in get_open_positions()
                 if len(p.get("symbol", "")) > 6 and float(p.get("quantity", 0) or 0) != 0]
    print(f"\nWorking orders that would be cancelled: {len(orders)}")
    for o in orders:
        print(f"  {o.get('id')}: {o.get('side')} {o.get('quantity')}x {o.get('option_symbol', o.get('symbol'))} ({o.get('status')})")
    print(f"\nOpen positions that would be closed: {len(positions)}")
    for p in positions:
        qty = float(p.get("quantity", 0) or 0)
        print(f"  {p.get('symbol')}: {'LONG' if qty > 0 else 'SHORT'} {abs(int(qty))}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Emergency flatten-all / kill switch.")
    parser.add_argument("--force", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--status", action="store_true", help="Show what would be flattened, then exit.")
    parser.add_argument("--no-halt", action="store_true", help="Flatten but do NOT set the halt flag.")
    parser.add_argument("--reason", default="manual CLI", help="Reason recorded in logs/notifications.")
    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    if not args.force:
        show_status()
        confirm = input("Type FLATTEN to cancel all orders and close all positions: ").strip()
        if confirm != "FLATTEN":
            print("Aborted — nothing was changed.")
            return 1

    summary = flatten_all(reason=args.reason, set_halt_flag=not args.no_halt)
    print("\n=== KILL SWITCH SUMMARY ===")
    print(f"  Orders cancelled: {summary.get('orders_cancelled', 0)}")
    pos = summary.get("positions", {})
    print(f"  Positions closed: {pos.get('closed', 0)} (failed: {pos.get('failed', 0)})")
    for d in pos.get("details", []):
        print(f"    {d}")
    print(f"  Halt flag set: {summary.get('halted')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
