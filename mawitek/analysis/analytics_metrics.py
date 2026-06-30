"""
analytics_metrics.py — portfolio performance metrics for the dashboard.

Computes the numbers a trader actually uses to judge a strategy, from the two
data files the bot already keeps:

    equity_curve.json   → Sharpe ratio, max drawdown, total return
    closed_trades.json  → win rate, profit factor, expectancy, avg win/loss,
                          and a per-strategy breakdown

All pure functions, no network. Used by dashboard_state.write_dashboard_state()
to embed a "metrics" block in dashboard_state.json, and runnable standalone:

    python analytics_metrics.py
"""

from __future__ import annotations

import math
from collections import defaultdict


# ─── Equity-curve metrics ─────────────────────────────────────────────────────

def _daily_equity(curve: list[dict]) -> list[tuple[str, float]]:
    """Collapse the intraday equity curve to one (date, last_equity) per day."""
    by_day: dict[str, float] = {}
    for pt in curve:
        d = pt.get("date") or (pt.get("timestamp", "")[:10])
        eq = float(pt.get("equity", 0) or 0)
        if d and eq > 0:
            by_day[d] = eq   # last write for the day wins
    return sorted(by_day.items())


def sharpe_ratio(curve: list[dict], risk_free_daily: float = 0.0) -> float | None:
    """
    Annualised Sharpe from daily equity returns. None if <2 days of data.
    """
    daily = _daily_equity(curve)
    if len(daily) < 2:
        return None
    rets = []
    for (_, prev), (_, cur) in zip(daily, daily[1:]):
        if prev > 0:
            rets.append((cur - prev) / prev - risk_free_daily)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(252)


def max_drawdown(curve: list[dict]) -> dict:
    """
    Largest peak-to-trough decline on the equity curve.
    Returns {"pct": float, "peak": float, "trough": float}.
    """
    peak = float("-inf")
    max_dd = 0.0
    peak_val = trough_val = 0.0
    cur_peak = float("-inf")
    for pt in curve:
        eq = float(pt.get("equity", 0) or 0)
        if eq <= 0:
            continue
        if eq > cur_peak:
            cur_peak = eq
        dd = (cur_peak - eq) / cur_peak if cur_peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            peak_val = cur_peak
            trough_val = eq
    return {"pct": round(max_dd * 100, 2), "peak": round(peak_val, 2), "trough": round(trough_val, 2)}


def total_return(curve: list[dict]) -> float | None:
    daily = _daily_equity(curve)
    if len(daily) < 2:
        # fall back to first/last raw points
        pts = [float(p.get("equity", 0) or 0) for p in curve if float(p.get("equity", 0) or 0) > 0]
        if len(pts) < 2:
            return None
        first, last = pts[0], pts[-1]
    else:
        first, last = daily[0][1], daily[-1][1]
    if first <= 0:
        return None
    return round((last - first) / first * 100, 2)


# ─── Closed-trade metrics ─────────────────────────────────────────────────────

def trade_metrics(trades: list[dict]) -> dict:
    """
    Win rate, profit factor, expectancy, avg win/loss from closed trades.

    profit_factor = gross wins / gross losses (>1 is profitable; inf if no losses)
    expectancy    = average P&L per trade (dollars)
    """
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "profit_factor": None, "expectancy": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        }

    pnls = [float(t.get("pnl_dollar", 0) or 0) for t in trades]
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    # No losing trades → profit factor is undefined/infinite. We deliberately
    # use None, NOT float("inf"): json.dump serializes inf to the literal
    # `Infinity`, which is invalid JSON and makes the browser's JSON.parse throw
    # — silently breaking the whole dashboard. The UI renders ∞ when
    # wins>0 and losses==0; None otherwise.
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    return {
        "count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "profit_factor": profit_factor,
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }


def per_strategy_metrics(trades: list[dict]) -> dict[str, dict]:
    """trade_metrics broken out per strategy name."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[t.get("strategy", "unknown")].append(t)
    return {strat: trade_metrics(ts) for strat, ts in groups.items()}


def per_trade_type_metrics(trades: list[dict]) -> dict[str, dict]:
    """trade_metrics split into day vs swing trades."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        # Fall back to classifying by strategy if an older record lacks the tag.
        tt = t.get("trade_type")
        if not tt:
            strat = t.get("strategy", "")
            tt = "day" if strat == "hft_intraday" else "swing"
        groups[tt].append(t)
    return {tt: trade_metrics(ts) for tt, ts in groups.items()}


# ─── Top-level ────────────────────────────────────────────────────────────────

def compute_metrics(curve: list[dict], trades: list[dict]) -> dict:
    """Everything the dashboard's metrics card needs, in one dict."""
    sharpe = sharpe_ratio(curve)
    return {
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown": max_drawdown(curve),
        "total_return_pct": total_return(curve),
        "trades": trade_metrics(trades),
        "by_strategy": per_strategy_metrics(trades),
        "by_trade_type": per_trade_type_metrics(trades),
    }


if __name__ == "__main__":
    import json
    from mawitek.core.trade_journal import load_closed_trades
    from mawitek.core.equity_tracker import load_equity_curve

    m = compute_metrics(load_equity_curve(), load_closed_trades())
    print(json.dumps(m, indent=2, default=str))
