"""
walk_forward.py — out-of-sample validation and live-vs-backtest divergence.

A backtest that looks great on the data you tuned it on tells you almost
nothing — that's curve-fitting. This module answers the two questions that
actually matter:

  1. WALK-FORWARD: split the trade history into sequential time windows and
     compare early-period ("in-sample") performance to later-period
     ("out-of-sample") performance. If the strategy only worked in the period
     you built it on, the out-of-sample numbers collapse — and you'll see it.

  2. DIVERGENCE: compare the bot's LIVE results to its BACKTEST baseline. A big
     gap means slippage, stale signals, or a strategy that's stopped working.

Both operate on the same trade-dict shape the bot already produces
(closed_trades.json) and that the backtesters emit:

    {"pnl_dollar": float, "exit_time"/"date": ISO str, "strategy": str, ...}

Pure functions, no network. CLI at the bottom.
"""

from __future__ import annotations

from analytics_metrics import trade_metrics


# ─── Time ordering / splitting ────────────────────────────────────────────────

def _trade_time(t: dict) -> str:
    """Best-effort sortable timestamp for a trade record."""
    return str(t.get("exit_time") or t.get("close_date") or t.get("date") or "")


def split_sequential(trades: list[dict], n_windows: int) -> list[list[dict]]:
    """
    Sort trades by time and split into `n_windows` contiguous chunks of
    roughly equal size. Order is preserved so window 0 is the oldest.
    """
    if n_windows < 1:
        n_windows = 1
    ordered = sorted(trades, key=_trade_time)
    n = len(ordered)
    if n == 0:
        return [[] for _ in range(n_windows)]
    size = n / n_windows
    out = []
    for i in range(n_windows):
        start = int(round(i * size))
        end = int(round((i + 1) * size))
        out.append(ordered[start:end])
    return out


# ─── Walk-forward evaluation ──────────────────────────────────────────────────

def evaluate_walk_forward(trades: list[dict], n_windows: int = 4) -> dict:
    """
    Split trades into `n_windows` sequential windows and compute metrics for
    each. Also computes an in-sample (first half) vs out-of-sample (second
    half) comparison and a simple degradation flag.

    Returns:
        {
          "n_windows": int,
          "windows": [ {window, count, win_rate, profit_factor, expectancy,
                        total_pnl}, ... ],
          "in_sample":  {...metrics...},
          "out_sample": {...metrics...},
          "degradation": {
              "expectancy_drop_pct": float | None,
              "win_rate_drop_pts": float | None,
              "flag": "ok" | "degraded" | "insufficient_data",
          }
        }
    """
    windows = split_sequential(trades, n_windows)
    win_reports = []
    for i, w in enumerate(windows):
        m = trade_metrics(w)
        win_reports.append({
            "window": i + 1,
            "count": m["count"],
            "win_rate": m["win_rate"],
            "profit_factor": m["profit_factor"],
            "expectancy": m["expectancy"],
            "total_pnl": round(m["gross_profit"] - m["gross_loss"], 2),
        })

    # In-sample = first half of the ordered trades; out-of-sample = second half.
    ordered = sorted(trades, key=_trade_time)
    mid = len(ordered) // 2
    in_sample = trade_metrics(ordered[:mid])
    out_sample = trade_metrics(ordered[mid:])

    degradation = _degradation(in_sample, out_sample)

    return {
        "n_windows": n_windows,
        "windows": win_reports,
        "in_sample": in_sample,
        "out_sample": out_sample,
        "degradation": degradation,
    }


def _degradation(in_s: dict, out_s: dict) -> dict:
    """Quantify how much performance dropped from in-sample to out-of-sample."""
    if in_s["count"] < 5 or out_s["count"] < 5:
        return {"expectancy_drop_pct": None, "win_rate_drop_pts": None, "flag": "insufficient_data"}

    exp_in, exp_out = in_s["expectancy"], out_s["expectancy"]
    if exp_in != 0:
        exp_drop = (exp_in - exp_out) / abs(exp_in) * 100
    else:
        exp_drop = 0.0
    wr_drop = in_s["win_rate"] - out_s["win_rate"]

    # Flag as degraded if out-of-sample expectancy turned negative, or dropped
    # more than 50%, or win rate fell more than 15 points.
    flag = "ok"
    if exp_out < 0 <= exp_in or exp_drop > 50 or wr_drop > 15:
        flag = "degraded"

    return {
        "expectancy_drop_pct": round(exp_drop, 1),
        "win_rate_drop_pts": round(wr_drop, 1),
        "flag": flag,
    }


# ─── Live vs backtest divergence ──────────────────────────────────────────────

def live_vs_backtest(live_trades: list[dict], backtest_trades: list[dict]) -> dict:
    """
    Compare live trading results against the backtest baseline.

    Returns per-metric live/backtest values plus a divergence flag. A large
    negative gap (live much worse than backtest) usually means slippage or
    signal decay.
    """
    live = trade_metrics(live_trades)
    back = trade_metrics(backtest_trades)

    def gap(a, b):
        if a is None or b is None:
            return None
        return round(a - b, 2)

    wr_gap = gap(live["win_rate"], back["win_rate"])
    exp_gap = gap(live["expectancy"], back["expectancy"])

    flag = "insufficient_data"
    if live["count"] >= 10:
        flag = "ok"
        if (exp_gap is not None and exp_gap < 0 and back["expectancy"] > 0
                and abs(exp_gap) > 0.5 * abs(back["expectancy"])):
            flag = "underperforming"
        elif wr_gap is not None and wr_gap < -15:
            flag = "underperforming"

    return {
        "live": live,
        "backtest": back,
        "win_rate_gap_pts": wr_gap,
        "expectancy_gap": exp_gap,
        "flag": flag,
    }


# ─── Pretty-print ─────────────────────────────────────────────────────────────

def format_walk_forward(report: dict) -> str:
    lines = [f"Walk-forward analysis ({report['n_windows']} windows)", ""]
    lines.append(f"{'Window':<8}{'Trades':<8}{'WinRate':<10}{'PF':<8}{'Expectancy':<12}{'Net P&L':<10}")
    for w in report["windows"]:
        pf = w["profit_factor"]
        pf_s = "∞" if pf in (None, float("inf")) and w["count"] else (str(pf) if pf is not None else "n/a")
        lines.append(
            f"{w['window']:<8}{w['count']:<8}{str(w['win_rate'])+'%':<10}{pf_s:<8}"
            f"{('+' if w['expectancy']>=0 else '')+str(w['expectancy']):<12}{w['total_pnl']:<10}"
        )
    d = report["degradation"]
    lines += ["", f"In-sample expectancy:     ${report['in_sample']['expectancy']}",
              f"Out-of-sample expectancy: ${report['out_sample']['expectancy']}",
              f"Degradation flag: {d['flag'].upper()}"]
    if d["expectancy_drop_pct"] is not None:
        lines.append(f"  Expectancy drop: {d['expectancy_drop_pct']}% | Win-rate drop: {d['win_rate_drop_pts']} pts")
    if d["flag"] == "degraded":
        lines.append("  ⚠ Strategy underperforms out-of-sample — likely curve-fit. Re-validate before trusting it.")
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    from state_io import read_json

    parser = argparse.ArgumentParser(description="Walk-forward / divergence analysis on a trade list.")
    parser.add_argument("--trades", default="closed_trades.json",
                        help="JSON file with a list of trade dicts (default: closed_trades.json).")
    parser.add_argument("--windows", type=int, default=4, help="Number of walk-forward windows.")
    parser.add_argument("--strategy", default=None, help="Filter to a single strategy.")
    parser.add_argument("--vs", default=None,
                        help="Backtest trades JSON to compare LIVE --trades against (divergence mode).")
    args = parser.parse_args()

    trades = read_json(args.trades, [])
    if not isinstance(trades, list):
        trades = trades.get("trades", []) if isinstance(trades, dict) else []
    if args.strategy:
        trades = [t for t in trades if t.get("strategy") == args.strategy]

    if not trades:
        print(f"No trades found in {args.trades}"
              + (f" for strategy {args.strategy}" if args.strategy else ""))
        raise SystemExit(0)

    if args.vs:
        backtest = read_json(args.vs, [])
        if isinstance(backtest, dict):
            backtest = backtest.get("trades", [])
        rep = live_vs_backtest(trades, backtest)
        print(json.dumps(rep, indent=2, default=str))
    else:
        rep = evaluate_walk_forward(trades, args.windows)
        print(format_walk_forward(rep))
