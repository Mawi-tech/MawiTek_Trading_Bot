"""
trade_journal.py

Persistent journal of every closed trade.

position_manager.remove_position() used to delete positions outright,
destroying the trade history. This module captures the full lifecycle
record BEFORE the delete so the dashboard's Trade History tab and the
Analytics tab have real data to work with.

Storage: closed_trades.json (a list of trade dicts, append-only).
"""

import json
import os
import datetime
from typing import Any

from mawitek.infra.state_io import file_lock, atomic_write_json
from mawitek.core.risk_manager import classify_trade_type
from mawitek.infra.utils import now_est, today_est, parse_isodt

CLOSED_TRADES_FILE = "closed_trades.json"


def load_closed_trades() -> list[dict]:
    """Return the full closed-trade history (newest last)."""
    if not os.path.exists(CLOSED_TRADES_FILE):
        return []
    try:
        with open(CLOSED_TRADES_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[TradeJournal] Could not read {CLOSED_TRADES_FILE}: {e}")
        return []


def record_closed_trade(
    option_symbol: str,
    underlying: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    expiration: str,
    entry_time: str | None,
    exit_reason: str,
    earnings_date: str | None = None,
    setup_score: float | None = None,
    signals: dict[str, Any] | None = None,
    strategy: str = "unknown",
    pnl_dollar: float | None = None,
    pnl_pct: float | None = None,
) -> dict:
    """
    Append a closed-trade record to closed_trades.json.

    P&L is computed here from entry/exit price so every downstream consumer
    reads the same number — that formula assumes a LONG single option.

    For multi-leg positions (credit spreads, straddles) the P&L sign and
    magnitude don't follow `(exit - entry) * qty` (a credit spread profits when
    you buy it back CHEAPER than the credit received). Those callers pass
    `pnl_dollar` and `pnl_pct` explicitly and we use them verbatim; here
    entry_price/exit_price are stored as the net credit/debit for reference.
    """
    if pnl_dollar is None:
        pnl_dollar = (exit_price - entry_price) * 100 * quantity  # options multiplier
    if pnl_pct is None:
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0

    # Hold duration in hours (best effort — entry_time may be missing for legacy positions)
    hold_hours: float | None = None
    if entry_time:
        try:
            entry_dt = parse_isodt(entry_time)   # tolerates naive legacy records
            hold_hours = round((now_est() - entry_dt).total_seconds() / 3600, 2)
        except Exception:
            pass

    # DTE at exit — anchored to US/Eastern so it doesn't drift when the host
    # is in a non-ET timezone (a UTC server would otherwise count 1 fewer day
    # for any trade closed in the evening ET).
    dte_at_exit: int | None = None
    try:
        exp = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        dte_at_exit = (exp - today_est()).days
    except Exception:
        pass

    record = {
        "option_symbol":  option_symbol,
        "underlying":     underlying,
        "strategy":       strategy,
        "trade_type":     classify_trade_type(strategy, dte_at_exit),
        "entry_price":    round(entry_price, 4),
        "exit_price":     round(exit_price, 4),
        "quantity":       quantity,
        "expiration":     expiration,
        "entry_time":     entry_time,
        "exit_time":      now_est().isoformat(),
        "hold_hours":     hold_hours,
        "dte_at_exit":    dte_at_exit,
        "pnl_dollar":     round(pnl_dollar, 2),
        "pnl_pct":        round(pnl_pct, 2),
        "exit_reason":    exit_reason,
        "earnings_date":  earnings_date,
        "setup_score":    setup_score,
        "signals":        signals or {},
    }

    # Lock the whole load→append→write so two strategy processes journaling a
    # close at the same moment can't both read the old list and one overwrite
    # the other's appended trade (lost-trade race).
    try:
        with file_lock(CLOSED_TRADES_FILE):
            trades = load_closed_trades()
            trades.append(record)
            atomic_write_json(CLOSED_TRADES_FILE, trades)
        print(
            f"[TradeJournal] Recorded closed trade | {option_symbol} | "
            f"P&L: ${pnl_dollar:+,.2f} ({pnl_pct:+.1f}%) | Reason: {exit_reason}"
        )
    except Exception as e:
        print(f"[TradeJournal] Failed to write closed trade: {e}")

    return record


def get_realized_pnl_today() -> float:
    """
    Sum of P&L for trades closed today. Used by risk_manager to compute
    a real daily P&L number instead of the cash-flow artifact it had before.

    "Today" is the US/Eastern trading day — same anchor record_closed_trade
    uses when writing exit_time — so this returns the correct number even on
    a host whose local clock disagrees with ET.
    """
    today = today_est().isoformat()
    total = 0.0
    for t in load_closed_trades():
        exit_time = t.get("exit_time", "")
        if isinstance(exit_time, str) and exit_time.startswith(today):
            total += float(t.get("pnl_dollar", 0) or 0)
    return total
