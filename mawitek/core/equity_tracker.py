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

from mawitek.data.tradier_client import get_account_balance, get_option_mid
from mawitek.core.position_manager import load_positions
from mawitek.infra.state_io import file_lock, atomic_write_json
from mawitek.infra.utils import now_est, today_est

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


def snapshot_equity() -> dict[str, Any] | None:
    """
    Capture and persist one equity-curve point. Called once per scan cycle.

    The snapshot includes everything the Analytics tab needs to draw the
    curve, plus the components so we can debug a weird number later.

    Returns None (and persists nothing) when the broker balance read is
    unusable — see the guard below.
    """
    balances = get_account_balance()
    cash = float(balances.get("cash", 0) or 0)
    reported_equity = float(balances.get("total_equity", 0) or 0)

    unrealized, market_value = calculate_unrealized_pnl()

    # The broker's total_equity is the only honest equity number we have.
    # When it's missing/zero, the balance read failed (get_account_balance
    # returns all-zeros on any API error) — and we CANNOT reconstruct it from
    # cash + market_value, because on a margin account get_account_balance
    # reports cash as 0. That reconstruction would record only the position
    # cost basis and silently drop all buying power. That is exactly what
    # poisoned the curve on 2026-06-23 (a bogus ~$13k point) and made daily
    # P&L read +$73k off it. Skip the snapshot rather than persist garbage.
    if reported_equity <= 0:
        print(
            "[EquityTracker] Skipping snapshot: broker returned no total_equity "
            f"(cash={cash}, market_value={market_value:.2f}) — likely a failed "
            "balance read; not persisting a corrupt equity point."
        )
        return None

    equity = reported_equity

    # Import here to avoid a circular import (trade_journal → nothing fancy,
    # but we import it lazily for symmetry with risk_manager).
    from mawitek.core.trade_journal import get_realized_pnl_today
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
        equity = float(record.get("equity", 0) or 0)
        # Skip non-positive equities — those are failed-read artifacts, not a
        # real baseline (defensive belt-and-braces alongside the write guard).
        if record.get("date") and record["date"] < today and equity > 0:
            return equity
    return None


def _last_known_equity() -> float | None:
    """Most recent snapshot equity (any date) with a positive value, else None."""
    for record in reversed(load_equity_curve()):
        equity = float(record.get("equity", 0) or 0)
        if equity > 0:
            return equity
    return None


def get_live_equity() -> float:
    """
    Current mark-to-market equity, computed without persisting a snapshot.
    Used inside the daily-loss check.
    """
    balances = get_account_balance()
    reported_equity = float(balances.get("total_equity", 0) or 0)

    if reported_equity > 0:
        return reported_equity

    # Broker read unusable (see snapshot_equity). Do NOT reconstruct from
    # cash + market_value — on a margin account cash reads as 0, so that would
    # collapse equity to the position cost basis and could false-trip the
    # daily-loss halt. Fall back to the last known good equity instead.
    last = _last_known_equity()
    if last is not None:
        return last

    # No history at all (very first cycle) → reconstruct as a last resort.
    cash = float(balances.get("cash", 0) or 0)
    _, market_value = calculate_unrealized_pnl()
    return cash + market_value
