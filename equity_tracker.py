"""
equity_tracker.py

True mark-to-market equity snapshots.

Why this module exists:
    risk_manager.calculate_daily_pnl() previously subtracted every buy
    and added every sell from today's orders. That treats every opening
    debit as a "loss" the instant a trade is placed, which:
      1. Made the dashboard's daily P&L number wrong every single day.
      2. Could false-trip the 5% daily-loss halt mid-session.

The fix is to value the account properly:

    equity_now      = cash + (sum of open positions at LIVE mid price)
    unrealized_pnl  = sum of (current_mid - entry_price) * qty * 100
    realized_today  = sum of P&L on trades CLOSED today (from trade_journal)
    daily_pnl       = equity_now - baseline_equity_for_today

The baseline is captured from the last snapshot dated BEFORE today,
so daily P&L includes both today's realized trades and any open-position
movement since yesterday's close — which is what a brokerage statement
would show.

Live quotes go through tradier_client.get_options_chain(). MOCK_MODE is
handled there (Black-Scholes synthetic chain), so this module needs no
mode-aware branching.
"""

import json
import os
from typing import Any

from tradier_client import get_account_balance, get_option_mid
from position_manager import load_positions
from state_io import file_lock, atomic_write_json
from utils import now_est, today_est

EQUITY_CURVE_FILE = "equity_curve.json"


# ─── Core Math ─────────────────────────────────────────────────────────────────

def calculate_unrealized_pnl() -> tuple[float, float]:
    """
    Returns (unrealized_pnl_dollars, total_market_value_of_open_positions).

    For any position whose live quote we can't fetch, we fall back to the
    entry price — i.e. we mark it flat rather than at zero. Marking at
    zero would let one bad quote falsely trip the daily-loss halt.
    """
    positions = load_positions()
    unrealized = 0.0
    market_value = 0.0

    for option_symbol, data in positions.items():
        underlying  = data.get("underlying", "")
        expiration  = data.get("expiration", "")
        entry_price = float(data.get("entry_price", 0) or 0)
        quantity    = int(data.get("quantity", 0) or 0)

        if entry_price <= 0 or quantity <= 0:
            continue

        current = get_option_mid(option_symbol, underlying, expiration)
        if current <= 0:
            current = entry_price  # quote unavailable → mark flat, do not flag a loss

        unrealized   += (current - entry_price) * quantity * 100
        market_value += current * quantity * 100

    return unrealized, market_value


def snapshot_equity() -> dict[str, Any]:
    """
    Capture and persist one equity-curve point. Called once per scan cycle.

    The snapshot includes everything the Analytics tab needs to draw the
    curve, plus the components so we can debug a weird number later.
    """
    balances = get_account_balance()
    cash = float(balances.get("cash", 0) or 0)
    reported_equity = float(balances.get("total_equity", 0) or 0)

    unrealized, market_value = calculate_unrealized_pnl()

    # Prefer the broker's total_equity when available — it's the most
    # honest number. Otherwise reconstruct it from cash + market value.
    if reported_equity > 0:
        equity = reported_equity
    else:
        equity = cash + market_value

    # Import here to avoid a circular import (trade_journal → nothing fancy,
    # but we import it lazily for symmetry with risk_manager).
    from trade_journal import get_realized_pnl_today
    realized_today = get_realized_pnl_today()

    record = {
        # ET-anchored so the "date" key matches what get_baseline_equity_for_today
        # looks up and what the rest of the bot calls "today".
        "timestamp":       now_est().isoformat(timespec="seconds"),
        "date":            today_est().isoformat(),
        "equity":          round(equity, 2),
        "cash":            round(cash, 2),
        "market_value":    round(market_value, 2),
        "unrealized_pnl":  round(unrealized, 2),
        "realized_today":  round(realized_today, 2),
    }

    _append_snapshot(record)
    return record


def _append_snapshot(record: dict) -> None:
    # Locked append so concurrent strategy processes don't lose snapshots.
    try:
        with file_lock(EQUITY_CURVE_FILE):
            curve = load_equity_curve()
            curve.append(record)
            atomic_write_json(EQUITY_CURVE_FILE, curve)
    except Exception as e:
        print(f"[EquityTracker] Could not persist snapshot: {e}")


def load_equity_curve() -> list[dict]:
    if not os.path.exists(EQUITY_CURVE_FILE):
        return []
    try:
        with open(EQUITY_CURVE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def get_baseline_equity_for_today() -> float | None:
    """
    Returns the most recent equity snapshot from BEFORE today.
    None if there's no prior snapshot (first day of operation).

    "Today" is the US/Eastern trading day so daily P&L doesn't reset hours
    early or late when the host clock disagrees with the market clock.
    """
    today = today_est().isoformat()
    curve = load_equity_curve()
    for record in reversed(curve):
        if record.get("date") and record["date"] < today:
            return float(record.get("equity", 0) or 0)
    return None


def get_live_equity() -> float:
    """
    Current mark-to-market equity, computed without persisting a snapshot.
    Used inside the daily-loss check.
    """
    balances = get_account_balance()
    reported_equity = float(balances.get("total_equity", 0) or 0)
    cash = float(balances.get("cash", 0) or 0)

    if reported_equity > 0:
        return reported_equity

    _, market_value = calculate_unrealized_pnl()
    return cash + market_value
