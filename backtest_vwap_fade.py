"""
backtest_vwap_fade.py  —  Strategy 6: VWAP Mean-Reversion Fade backtester

The go-live gate for ENABLE_VWAP_FADE. Backtests the vwap_fade_scanner signal on
historical intraday data with the SAME theta-honest Black-Scholes option model as
backtest_hft.py, and the SAME reversion-tuned exits as vwap_fade_executor
(30-min time-stop, VWAP-touch TP, further-stretch invalidation, +40%/−25% premium
TP/SL). It reports a CALL-side vs PUT-side P&L split — the bidirectional-honesty
check that rejected the earlier HFT-bidirectional / PEAD-short experiments.

Because it is a bidirectional intraday edge, per repo convention it must be
POSITIVE on TWO INDEPENDENT SAMPLES before ENABLE_VWAP_FADE is flipped (ARCH §1):

    Sample A (mega/large caps):  python backtest_vwap_fade.py --days 59
    Sample B (broad S&P):        python backtest_vwap_fade.py --universe --max-tickers 150 --days 59

ACCEPTANCE (must hold on BOTH samples):
    total P&L > 0,  profit factor >= 1.20,  win rate >= 45%,  >= 100 trades,
    and the smaller side's P&L must not be a large net loss (see per-side check).

yfinance only serves ~59 days of 5-minute history, so the window is shorter than
the swing backtests — the mega basket is broadened to accumulate enough trades.

Reuses the theta-honest pricing / data / HV core from backtest_hft.
"""

import argparse
import datetime

import numpy as np
import pandas as pd

import vwap_fade_scanner as vf
from vwap_fade_scanner import compute_vwap, detect_vwap_fade, in_fade_session, INVERSE_ETF_LIST
from backtest_hft import (
    _bs_price, _get_intraday_yf, _get_hist_iv, _interval_minutes, fetch_sp500_tickers,
)
from market_data import get_intraday_bars
import vwap_fade_executor as ex


# --- Config (mirror the executor's exits so the backtest validates LIVE logic) --

STARTING_CAPITAL      = 50_000
RISK_PER_TRADE_PCT    = 0.01          # 1% per trade (matches FADE_SIZE_PCT_HIGH)
DEFAULT_DAYS          = 59
DEFAULT_INTERVAL      = "5m"
MIN_SCORE             = ex.MIN_SETUP_SCORE

MAX_HOLD_BARS         = ex.MAX_HOLD_MINUTES // 5   # 30 min / 5m = 6 bars
TAKE_PROFIT_PCT       = ex.TAKE_PROFIT_PCT         # +40%
STOP_LOSS_PCT         = ex.STOP_LOSS_PCT           # -25%
VWAP_TOUCH_BAND_PCT   = ex.VWAP_TOUCH_BAND_PCT     # 0.10%
STRETCH_STOP_PCT      = ex.STRETCH_STOP_PCT        # 0.50%
MIN_BARS_BETWEEN_ENTRIES = 3
COMMISSION_PER_LEG    = 0.65
SLIPPAGE_PCT          = 0.02

# Mega/large-cap sample A — liquid, actively-mean-reverting names (broadened from
# HFT's 5-name default to accumulate trades in the ~59d 5m window).
MEGA_UNIVERSE = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "AMD",
    "AVGO", "NFLX", "JPM", "XOM", "UNH", "COST", "HD", "BAC", "DIS", "CRM",
]

# Acceptance-gate thresholds.
GATE_MIN_PF        = 1.20
GATE_MIN_WIN_RATE  = 45.0
GATE_MIN_TRADES    = 100


# --- Single-ticker backtest ----------------------------------------------------

def backtest_ticker(ticker: str, days: int = DEFAULT_DAYS, interval: str = DEFAULT_INTERVAL,
                    starting_capital: float = STARTING_CAPITAL, min_score: int = MIN_SCORE,
                    show_trades: bool = False) -> list[dict]:
    """Return the list of simulated fade trades for one ticker."""
    if ticker.upper() in INVERSE_ETF_LIST:
        return []

    df = _get_intraday_yf(ticker, days, interval)
    if df is None or len(df) < vf.MIN_BARS:
        df = get_intraday_bars(ticker, interval=interval, days=days)
    if df is None or df.empty or len(df) < vf.MIN_BARS:
        return []

    equity = starting_capital
    trades: list[dict] = []
    last_entry_bar = -MIN_BARS_BETWEEN_ENTRIES
    bar_minutes = _interval_minutes(interval)
    bar_days    = bar_minutes / (60.0 * 24.0)

    for i in range(vf.MIN_BARS, len(df) - 1):
        if i - last_entry_bar < MIN_BARS_BETWEEN_ENTRIES:
            continue
        bar_time = df.index[i]
        if not in_fade_session(bar_time):
            continue

        today  = df.index[i].date()
        window = df.iloc[:i + 1]
        window = window[window.index.date == today]
        if len(window) < vf.MIN_BARS:
            continue

        vwap = compute_vwap(window)
        sig  = detect_vwap_fade(window, vwap)
        if not sig["signal"] or sig["score"] < min_score:
            continue

        direction  = sig["direction"]
        kind       = "call" if direction == "bullish" else "put"
        entry_price = float(df.iloc[i]["Close"])
        entry_vwap  = float(vwap.iloc[-1])
        if entry_price <= 0 or entry_vwap <= 0:
            continue
        entry_pct = (entry_price - entry_vwap) / entry_vwap * 100
        iv = _get_hist_iv(window["Close"])

        # Same-day expiry (0-DTE realism): option life = minutes to 16:00 ET.
        mins_to_close  = max(bar_minutes, (16 * 60) - (bar_time.hour * 60 + bar_time.minute))
        entry_dte_days = mins_to_close / (60.0 * 24.0)
        entry_val = _bs_price(entry_price, entry_price, iv, entry_dte_days, kind)
        if entry_val <= 0:
            continue

        budget = equity * RISK_PER_TRADE_PCT
        quantity = max(1, int(budget // (entry_val * 100)))

        # Precompute the session VWAP so forward bars can measure reversion.
        session = df[df.index.date == today]
        session_vwap = compute_vwap(session)

        exit_idx, exit_reason = None, "time_stop"
        for j in range(1, MAX_HOLD_BARS + 1):
            if i + j >= len(df):
                break
            fbar = df.index[i + j]
            if fbar.date() != today:      # crossed into next session — treat as EOD
                break
            fclose = float(df.iloc[i + j]["Close"])
            fvwap  = float(session_vwap.get(fbar, entry_vwap))
            if fvwap <= 0:
                fvwap = entry_vwap
            fpct = (fclose - fvwap) / fvwap * 100

            # Further-stretch invalidation (protective) — accelerating away from VWAP.
            if entry_pct != 0 and (fpct - entry_pct) * (1 if entry_pct > 0 else -1) >= STRETCH_STOP_PCT:
                exit_idx, exit_reason = i + j, "further_stretch"
                break
            # VWAP-touch TP — reverted to fair value, thesis complete.
            if abs(fpct) <= VWAP_TOUCH_BAND_PCT:
                exit_idx, exit_reason = i + j, "vwap_touch"
                break
            # Premium TP / SL (theta-aware option value).
            dte_j = max(1e-6, entry_dte_days - j * bar_days)
            val_j = _bs_price(fclose, entry_price, iv, dte_j, kind)
            opt_pct = (val_j - entry_val) / entry_val
            if opt_pct >= TAKE_PROFIT_PCT:
                exit_idx, exit_reason = i + j, "take_profit"
                break
            if opt_pct <= -STOP_LOSS_PCT:
                exit_idx, exit_reason = i + j, "stop_loss"
                break

        if exit_idx is None:
            exit_idx = min(i + MAX_HOLD_BARS, len(df) - 1)
        held_bars  = exit_idx - i
        exit_price = float(df.iloc[exit_idx]["Close"])
        exit_dte   = max(1e-6, entry_dte_days - held_bars * bar_days)
        exit_val   = _bs_price(exit_price, entry_price, iv, exit_dte, kind)

        gross      = (exit_val - entry_val) * 100 * quantity
        commission = COMMISSION_PER_LEG * quantity * 2
        slippage   = entry_val * 100 * quantity * SLIPPAGE_PCT
        pnl = round(gross - commission - slippage, 2)
        equity = max(0, equity + pnl)
        last_entry_bar = i

        trades.append({
            "ticker": ticker, "entry_time": df.index[i], "exit_time": df.index[exit_idx],
            "direction": direction, "side": kind, "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2), "quantity": quantity, "pnl": pnl,
            "exit_reason": exit_reason, "score": sig["score"], "conviction": sig["conviction"],
            "stretch_z": sig["z"],
        })
        if show_trades:
            print(f"  {df.index[i].strftime('%m/%d %H:%M')}->{df.index[exit_idx].strftime('%H:%M')} | "
                  f"{direction:8s} {kind} | z={sig['z']:+.1f} score={sig['score']} | "
                  f"${entry_price:.2f}->${exit_price:.2f} | ${pnl:+.2f} | {exit_reason}")

    return trades


# --- Metrics -------------------------------------------------------------------

def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "profit_factor": 0.0, "avg_pnl": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    gross_w = sum(wins)
    gross_l = abs(sum(p for p in pnls if p <= 0))
    return {
        "trades": len(pnls), "wins": len(wins),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "total_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
    }


def run_backtest(tickers: list[str], days: int = DEFAULT_DAYS, interval: str = DEFAULT_INTERVAL,
                 min_score: int = MIN_SCORE, show_trades: bool = False) -> dict:
    print("\n" + "=" * 72)
    print("  VWAP MEAN-REVERSION FADE BACKTEST — Strategy 6")
    print(f"  {len(tickers)} tickers | {days}d | {interval} | TP +{TAKE_PROFIT_PCT:.0%} "
          f"SL -{STOP_LOSS_PCT:.0%} | hold {MAX_HOLD_BARS} bars")
    print("=" * 72)

    all_trades: list[dict] = []
    for t in tickers:
        tr = backtest_ticker(t, days=days, interval=interval, min_score=min_score, show_trades=show_trades)
        if tr:
            s = _stats(tr)
            print(f"[{t:6s}] {s['trades']:3d} trades | WR {s['win_rate']:5.1f}% | "
                  f"PF {s['profit_factor'] if s['profit_factor']!=float('inf') else 'inf':>5} | ${s['total_pnl']:+.2f}")
        all_trades.extend(tr)

    overall = _stats(all_trades)
    calls   = _stats([t for t in all_trades if t["side"] == "call"])
    puts    = _stats([t for t in all_trades if t["side"] == "put"])

    print("\n" + "=" * 72)
    print("  RESULTS")
    print("=" * 72)
    for name, s in (("Overall", overall), ("  └ CALL side (fade flushes)", calls),
                    ("  └ PUT side (fade rips)", puts)):
        pf = s["profit_factor"]; pf_txt = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(f"  {name:30s} {s['trades']:4d} trades | WR {s['win_rate']:5.1f}% | "
              f"PF {pf_txt:>5s} | avg ${s['avg_pnl']:+8.2f} | total ${s['total_pnl']:+11.2f}")

    # Per-side honesty: the smaller side must not be a large net loss (a strategy
    # that only works one direction is the failure mode that rejected prior
    # bidirectional experiments). "Large" = worse than -25% of the winning side.
    big = max(calls["total_pnl"], puts["total_pnl"], 0.0)
    small = min(calls["total_pnl"], puts["total_pnl"])
    side_ok = small >= -0.25 * big if big > 0 else False

    checks = [
        ("total P&L > 0",              overall["total_pnl"] > 0),
        (f"profit factor >= {GATE_MIN_PF}", overall["profit_factor"] >= GATE_MIN_PF),
        (f"win rate >= {GATE_MIN_WIN_RATE:.0f}%", overall["win_rate"] >= GATE_MIN_WIN_RATE),
        (f">= {GATE_MIN_TRADES} trades", overall["trades"] >= GATE_MIN_TRADES),
        ("per-side honesty (weak side not a big loss)", side_ok),
    ]
    print("\n  ACCEPTANCE GATE (this sample — must also pass the second sample):")
    for label, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}")
    sample_pass = all(ok for _, ok in checks)
    print(f"\n  SAMPLE {'PASSED' if sample_pass else 'FAILED'} — "
          f"{'run the other sample, then flip ENABLE_VWAP_FADE if both pass' if sample_pass else 'ENABLE_VWAP_FADE stays False'}")
    print("=" * 72 + "\n")

    return {"overall": overall, "calls": calls, "puts": puts,
            "sample_pass": sample_pass, "n_trades": overall["trades"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VWAP Mean-Reversion Fade Backtest — Strategy 6")
    parser.add_argument("--tickers", nargs="+", default=None, help="Explicit ticker list")
    parser.add_argument("--universe", action="store_true", help="Use the broad S&P 500 basket (Sample B)")
    parser.add_argument("--max-tickers", type=int, default=150)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--show-trades", action="store_true")
    args = parser.parse_args()

    if args.tickers:
        universe = args.tickers
    elif args.universe:
        universe = fetch_sp500_tickers(max_tickers=args.max_tickers)
    else:
        universe = MEGA_UNIVERSE

    run_backtest(universe, days=args.days, interval=args.interval,
                 min_score=args.min_score, show_trades=args.show_trades)
