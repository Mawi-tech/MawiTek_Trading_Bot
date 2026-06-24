"""
position_manager.py — exit management for the catalyst (Strategy 1) book.

Tracks the catalyst strategy's open long-call positions in
open_positions.json and manages their exits. The other strategies keep
their own books (hft_positions.json, iv_rank_positions.json,
pead_positions.json, bounce_positions.json) with strategy-specific exit
logic.

Exit rules for this book:
    - +TAKE_PROFIT_PCT (currently +100%)  → close at double-up
    - -STOP_LOSS_PCT   (currently  -50%)  → cut losers
    - DTE ≤ FORCE_CLOSE_DTE (currently 1) → flat before expiry / pin risk
    - earnings_date passed                → catalyst played out, close

All exits go through Tradier sell_to_close orders. Multi-process safe:
record_entry/remove_position are wrapped in a cross-process file lock so
two strategies (or a reconciler) writing simultaneously can't corrupt or
lose entries.

Closed positions are journaled to closed_trades.json BEFORE the delete so
trade history survives a mid-close crash. record_entry stashes the setup
score + signal snapshot so the Trade History tab can later answer "what
did this setup look like at entry?"
"""

import json
import os
import datetime
from typing import Any
from tradier_client import place_option_order, get_option_mid
from trade_journal import record_closed_trade
from decision_log import log_decision, ACTION_EXITED
from utils import now_est, today_est
from event_notifier import notify_position_closed, notify_big_move
from state_io import file_lock, atomic_write_json


# ─── Exit Config ───────────────────────────────────────────────────────────────

TAKE_PROFIT_PCT   = 1.00    # Close at +100% (2x your money)
STOP_LOSS_PCT     = -0.50   # Close at -50% (standard for long options)
FORCE_CLOSE_DTE   = 1       # Force close with 1 day left to avoid pin risk

# File to persist entry prices across restarts
POSITIONS_FILE = "open_positions.json"


# ─── Position Storage ──────────────────────────────────────────────────────────

def load_positions() -> dict:
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_positions(positions: dict):
    atomic_write_json(POSITIONS_FILE, positions)


def record_entry(
    option_symbol: str,
    underlying: str,
    entry_price: float,
    quantity: int,
    expiration: str,
    earnings_date: str | None = None,
    setup_score: float | None = None,
    signals: dict[str, Any] | None = None,
    strategy: str = "unknown",
):
    """
    Record a new position entry.

    setup_score, signals, and strategy are stashed so the eventual
    closed-trade record can answer "what did this trade look like at
    entry?" and "which strategy did it come from?"
    """
    # Locked read-modify-write — another strategy process may be reconciling
    # or recording at the same moment.
    with file_lock(POSITIONS_FILE):
        positions = load_positions()
        positions[option_symbol] = {
            "underlying":    underlying,
            "entry_price":   entry_price,
            "quantity":      quantity,
            "expiration":    expiration,
            "earnings_date": earnings_date,
            # ET-anchored, tz-aware so hold-time math against now_est() works
            # without any local-clock surprises.
            "entry_time":    now_est().isoformat(),
            "setup_score":   setup_score,
            "signals":       signals or {},
            "strategy":      strategy,
        }
        save_positions(positions)
    print(f"[PositionManager] Recorded entry: {option_symbol} @ ${entry_price:.2f} x{quantity} ({strategy})")


def remove_position(
    option_symbol: str,
    exit_price: float | None = None,
    exit_reason: str = "manual_close",
):
    """
    Remove a position from the open book, journaling it first.

    If exit_price is None we fall back to the last known mid price, then
    finally to entry price — so we never crash here just because the quote
    feed hiccuped at the moment of exit.
    """
    positions = load_positions()
    if option_symbol not in positions:
        return

    data = positions[option_symbol]

    # Resolve an exit price we can actually journal. Do the (network) quote
    # lookup BEFORE taking the lock so we never hold the position lock during I/O.
    if exit_price is None or exit_price <= 0:
        try:
            exit_price = get_option_mid(
                option_symbol,
                data.get("underlying", ""),
                data.get("expiration", ""),
            )
        except Exception:
            exit_price = 0.0
    if not exit_price or exit_price <= 0:
        exit_price = float(data.get("entry_price", 0) or 0)

    # Atomically claim the position: under the lock, re-check it still exists
    # (another process may have closed it first), journal it, then delete.
    # Journaling before the delete preserves history if we crash mid-way, and
    # the existence re-check guarantees exactly one caller journals it.
    with file_lock(POSITIONS_FILE):
        positions = load_positions()
        if option_symbol not in positions:
            return  # already handled by another process
        data = positions[option_symbol]
        record_closed_trade(
            option_symbol = option_symbol,
            underlying    = data.get("underlying", ""),
            entry_price   = float(data.get("entry_price", 0) or 0),
            exit_price    = float(exit_price),
            quantity      = int(data.get("quantity", 0) or 0),
            expiration    = data.get("expiration", ""),
            entry_time    = data.get("entry_time"),
            exit_reason   = exit_reason,
            earnings_date = data.get("earnings_date"),
            setup_score   = data.get("setup_score"),
            signals       = data.get("signals"),
            strategy      = data.get("strategy", "unknown"),
        )
        del positions[option_symbol]
        save_positions(positions)

    # Everything below is logging / notification — slow or network-bound, so
    # it runs OUTSIDE the position lock.
    log_decision(
        ticker = data.get("underlying", "?"),
        action = ACTION_EXITED,
        reason = exit_reason,
        extras = {
            "option_symbol": option_symbol,
            "exit_price":    round(float(exit_price), 4),
        },
        force = True,   # never collapse a position exit out of the audit log
    )

    entry_px = float(data.get("entry_price", 0) or 0)
    qty      = int(data.get("quantity", 0) or 0)
    pnl_dollar = (float(exit_price) - entry_px) * qty * 100
    pnl_pct    = ((float(exit_price) - entry_px) / entry_px * 100) if entry_px > 0 else 0.0
    try:
        notify_position_closed(
            ticker   = data.get("underlying", "?"),
            contract = option_symbol,
            pnl_dollar = round(pnl_dollar, 2),
            pnl_pct    = round(pnl_pct, 1),
            reason   = exit_reason,
            strategy = data.get("strategy", "unknown"),
        )
    except Exception as e:
        print(f"[PositionManager] notify_position_closed failed: {e}")


# ─── Current Price ─────────────────────────────────────────────────────────────

# ─── Exit Logic ────────────────────────────────────────────────────────────────

def days_until_expiry(expiration: str) -> int:
    try:
        exp_date = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        # Compare against the US/Eastern trading date so DTE math doesn't
        # drift when the host machine is in a different timezone.
        return (exp_date - today_est()).days
    except Exception:
        return 999


def earnings_have_passed(earnings_date: str | None) -> bool:
    if not earnings_date:
        return False
    try:
        edate = datetime.datetime.strptime(earnings_date, "%Y-%m-%d").date()
        return today_est() > edate
    except Exception:
        return False


def should_exit(
    option_symbol: str,
    entry_price: float,
    current_price: float,
    expiration: str,
    earnings_date: str | None
) -> tuple[bool, str]:
    """
    Determine if we should exit a position.

    Returns:
        (should_exit: bool, reason: str)
    """
    if current_price <= 0:
        return False, "No price available"

    pnl_pct = (current_price - entry_price) / entry_price

    # Take profit
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, f"Take profit hit (+{pnl_pct*100:.1f}%)"

    # Stop loss
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"Stop loss hit ({pnl_pct*100:.1f}%)"

    # Force close near expiry
    dte = days_until_expiry(expiration)
    if dte <= FORCE_CLOSE_DTE:
        return True, f"Force close — only {dte} DTE remaining"

    # Post-earnings close
    if earnings_have_passed(earnings_date):
        return True, "Earnings catalyst played out — closing position"

    return False, ""


# ─── Main Monitor Loop ─────────────────────────────────────────────────────────

def monitor_positions():
    """
    Check all open positions and exit those that hit
    take profit, stop loss, expiry, or post-earnings criteria.
    """
    positions = load_positions()

    if not positions:
        print("[PositionManager] No open positions to monitor.")
        return

    print(f"\n[PositionManager] Monitoring {len(positions)} open positions...\n")

    for option_symbol, data in list(positions.items()):
        underlying    = data["underlying"]
        entry_price   = float(data["entry_price"])
        quantity      = int(data["quantity"])
        expiration    = data["expiration"]
        earnings_date = data.get("earnings_date")

        current_price = get_option_mid(option_symbol, underlying, expiration)
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if current_price > 0 else 0

        print(
            f"[PositionManager] {option_symbol} | "
            f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
            f"P&L: {pnl_pct:+.1f}% | DTE: {days_until_expiry(expiration)}"
        )

        # Alert on big moves (±20%+). Deduped per threshold bucket in-memory.
        if current_price > 0:
            try:
                notify_big_move(
                    ticker        = underlying,
                    option_symbol = option_symbol,
                    contract      = option_symbol,
                    pnl_pct       = pnl_pct,
                    entry_price   = entry_price,
                    current_price = current_price,
                )
            except Exception as e:
                print(f"[PositionManager] notify_big_move failed: {e}")

        exit_now, reason = should_exit(
            option_symbol, entry_price, current_price, expiration, earnings_date
        )

        if exit_now:
            print(f"[PositionManager] 🚪 Exiting {option_symbol} — {reason}")

            order = place_option_order(
                symbol=underlying,
                option_symbol=option_symbol,
                side="sell_to_close",
                quantity=quantity,
                order_type="market",
            )

            if order.get("status") not in ("error", None):
                # Pass the price we exited at + the trigger that fired, so
                # closed_trades.json captures the real exit, not just a default.
                remove_position(
                    option_symbol,
                    exit_price=current_price,
                    exit_reason=reason,
                )
                print(f"[PositionManager] ✅ Closed {option_symbol} | Reason: {reason}")
            else:
                print(f"[PositionManager] ❌ Failed to close {option_symbol}: {order}")
