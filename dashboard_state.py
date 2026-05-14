"""
dashboard_state.py

Writes bot state to dashboard_state.json after every scan cycle.
The dashboard reads this file to show real live data.

Exports:
- Account balances
- Open positions with live P&L
- Today's trades
- Latest scanner setups
- Risk state (daily P&L, halt status)
- Bot status
"""

import json
import datetime
import os

from tradier_client import get_account_balance, get_open_positions, get_orders_today, get_options_chain
from risk_manager import load_state, DAILY_LOSS_LIMIT_PCT, MAX_OPEN_POSITIONS
from position_manager import load_positions, days_until_expiry

STATE_FILE = "dashboard_state.json"


def get_option_mid_price(option_symbol: str, underlying: str, expiration: str) -> float:
    try:
        chain = get_options_chain(underlying, expiration)
        for contract in chain:
            if contract.get("symbol") == option_symbol:
                bid = float(contract.get("bid", 0) or 0)
                ask = float(contract.get("ask", 0) or 0)
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
    except Exception:
        pass
    return 0.0


def build_positions_data() -> list[dict]:
    """Build enriched position data with live P&L."""
    tracked = load_positions()
    results = []

    for option_symbol, data in tracked.items():
        underlying  = data.get("underlying", "")
        entry_price = float(data.get("entry_price", 0))
        quantity    = int(data.get("quantity", 1))
        expiration  = data.get("expiration", "")
        earnings_date = data.get("earnings_date")

        current_price = get_option_mid_price(option_symbol, underlying, expiration)
        if current_price <= 0:
            current_price = entry_price  # fallback to entry if no price

        pnl_pct    = round((current_price - entry_price) / entry_price * 100, 1) if entry_price > 0 else 0
        pnl_dollar = round((current_price - entry_price) * quantity * 100, 2)
        dte        = days_until_expiry(expiration)

        # Determine exit trigger label
        from position_manager import TAKE_PROFIT_PCT, STOP_LOSS_PCT, FORCE_CLOSE_DTE
        if pnl_pct / 100 >= TAKE_PROFIT_PCT * 0.85:
            exit_trigger = f"TP near (+{round(TAKE_PROFIT_PCT*100)}%)"
        elif pnl_pct / 100 <= STOP_LOSS_PCT * 0.85:
            exit_trigger = f"SL near ({round(STOP_LOSS_PCT*100)}%)"
        elif dte <= FORCE_CLOSE_DTE + 2:
            exit_trigger = f"Expiry in {dte}d"
        elif earnings_date:
            try:
                edate = datetime.datetime.strptime(earnings_date, "%Y-%m-%d").date()
                days_to_earn = (edate - datetime.date.today()).days
                if days_to_earn >= 0:
                    exit_trigger = f"Earnings in {days_to_earn}d"
                else:
                    exit_trigger = "Post-earnings close"
            except Exception:
                exit_trigger = "Watching"
        else:
            exit_trigger = "Watching"

        # Parse strike and expiry for display
        try:
            exp_display = datetime.datetime.strptime(expiration, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            exp_display = expiration

        results.append({
            "symbol":        option_symbol,
            "underlying":    underlying,
            "expiration":    expiration,
            "exp_display":   exp_display,
            "dte":           dte,
            "quantity":      quantity,
            "entry_price":   entry_price,
            "current_price": current_price,
            "pnl_pct":       pnl_pct,
            "pnl_dollar":    pnl_dollar,
            "exit_trigger":  exit_trigger,
            "earnings_date": earnings_date,
        })

    return results


def build_trades_data() -> list[dict]:
    """Build today's trade log from Tradier orders."""
    orders = get_orders_today()
    results = []

    for order in orders:
        status = order.get("status", "")
        side   = order.get("side", "")
        sym    = order.get("option_symbol", order.get("symbol", ""))
        price  = order.get("avg_fill_price", "—")
        qty    = order.get("quantity", 1)

        try:
            raw_time = order.get("transaction_date", "")
            time_str = datetime.datetime.fromisoformat(raw_time).strftime("%I:%M %p")
        except Exception:
            time_str = "—"

        underlying = sym[:4] if len(sym) > 6 else sym

        results.append({
            "ticker":  underlying,
            "action":  side.replace("_", " ").title() if side else "Unknown",
            "time":    time_str,
            "result":  "filled" if status == "filled" else ("risk" if status == "rejected" else status),
            "price":   price,
            "qty":     qty,
        })

    return results


def build_pnl_history() -> list[dict]:
    """
    Load rolling P&L history from persistent log.
    Appends today's P&L, keeps last 10 trading days.
    """
    history_file = "pnl_history.json"
    today = datetime.date.today().isoformat()

    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                history = json.load(f)
        except Exception:
            history = []

    # Update or append today
    risk_state = load_state()
    today_pnl  = round(risk_state.get("realized_pnl", 0.0), 2)

    existing = [h for h in history if h["date"] != today]
    existing.append({"date": today, "pnl": today_pnl})

    # Keep last 10 days
    history = sorted(existing, key=lambda x: x["date"])[-10:]

    with open(history_file, "w") as f:
        json.dump(history, f)

    return history


def write_dashboard_state(
    setups: list[dict] | None = None,
    bot_status: str = "running",
    account_mode: str = "paper",
):
    """
    Write full dashboard state to JSON.
    Call this at the end of every executor scan cycle.

    Args:
        setups:       List of scanner setup dicts from options_scanner
        bot_status:   "running", "halted", "scanning", "idle"
        account_mode: "paper" or "live"
    """
    try:
        balances   = get_account_balance()
        equity     = balances.get("total_equity", 0)
        risk_state = load_state()

        daily_pnl     = round(risk_state.get("realized_pnl", 0.0), 2)
        trades_today  = risk_state.get("trades_today", 0)
        halted        = risk_state.get("halted", False)
        loss_limit    = round(equity * DAILY_LOSS_LIMIT_PCT, 2)
        loss_used_pct = round(abs(daily_pnl) / loss_limit * 100, 1) if loss_limit > 0 and daily_pnl < 0 else 0

        positions     = build_positions_data()
        open_count    = len(positions)
        trades        = build_trades_data()
        pnl_history   = build_pnl_history()

        # Filter pass rate stats
        filter_stats = {"earnings": 0, "flow": 0, "news": 0, "momentum": 0, "total": 0}
        if setups:
            filter_stats["total"] = len(setups)
            for s in setups:
                if s.get("days_until_earnings") is not None: filter_stats["earnings"] += 1
                if s.get("options_flow"):                    filter_stats["flow"] += 1
                if s.get("news_catalyst"):                   filter_stats["news"] += 1
                if s.get("momentum_score", 0) >= 40:        filter_stats["momentum"] += 1

        def pct(n, total):
            return round(n / total * 100) if total > 0 else 0

        state = {
            "timestamp":    datetime.datetime.now().isoformat(),
            "account_mode": account_mode,
            "bot_status":   "halted" if halted else bot_status,
            "account": {
                "equity":          round(equity, 2),
                "daily_pnl":       daily_pnl,
                "daily_pnl_pct":   round(daily_pnl / equity * 100, 2) if equity > 0 else 0,
                "loss_limit":      loss_limit,
                "loss_used":       abs(daily_pnl) if daily_pnl < 0 else 0,
                "loss_used_pct":   loss_used_pct,
                "open_positions":  open_count,
                "max_positions":   MAX_OPEN_POSITIONS,
                "trades_today":    trades_today,
                "halted":          halted,
            },
            "positions":   positions,
            "trades":      trades,
            "pnl_history": pnl_history,
            "setups":      setups or [],
            "filter_stats": {
                "earnings_pct":  pct(filter_stats["earnings"],  filter_stats["total"]),
                "flow_pct":      pct(filter_stats["flow"],      filter_stats["total"]),
                "news_pct":      pct(filter_stats["news"],      filter_stats["total"]),
                "momentum_pct":  pct(filter_stats["momentum"],  filter_stats["total"]),
            },
        }

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        print(f"[Dashboard] State written -> {STATE_FILE}")

    except Exception as e:
        print(f"[Dashboard] Error writing state: {e}")
