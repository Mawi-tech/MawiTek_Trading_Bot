"""
backtest_bounce.py — Strategy 5: Capitulation-Bounce backtest.

The bot's bear-market OFFENSE. Our four bearish-drift experiments all lost because
oversold down-gaps MEAN-REVERT (snap back) in a bear market. So this trades the
contrarian side: in a BEAR regime (SPY < 200-day SMA), an oversold down-gap is
capitulation that bounces — so we BUY a short-dated call and ride the rebound.

Hard-gated to bear regimes: the very same setup LOSES in a bull market (a down-gap
there is genuine bad news that keeps falling), so the SPY-regime gate is what makes
this an edge rather than a coin flip.

Reuses backtest_pead's daily yfinance pipeline + Black-Scholes pricing, and
pead_scanner.detect_drift to locate the oversold down-gap (it already finds these
as "bearish" setups — we simply trade them long instead of short).

Run:
    python backtest_bounce.py --days 1460                 # default basket, 4yr
    python backtest_bounce.py --universe --max-tickers 90 # wider S&P sample
    python backtest_bounce.py --dte 14 --hold 6 --tp 0.6 --sl 0.35 --show-trades
"""

import argparse
import datetime

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import pead_scanner as ps
from pead_scanner import detect_drift
from backtest_pead import (
    _bs_price, _hist_vol, _load_spy_regime, DEFAULT_UNIVERSE,
    STARTING_CAPITAL, RISK_PER_TRADE_PCT, COMMISSION_PER_CONTRACT,
)

load_dotenv()


# ─── Config ──────────────────────────────────────────────────────────────────

DTE_ENTRY     = 14      # short-dated — a bounce is a days-long move, not weeks
TAKE_PROFIT   = 0.60    # +60% on the call
STOP_LOSS     = 0.35    # -35%
MAX_HOLD_DAYS = 6       # trading-day time stop (exit before the downtrend resumes)
COOLDOWN_DAYS = 5
SLIPPAGE_PCT  = 0.02    # 2% of the option mid, paid on entry


def backtest_ticker(ticker, df, spy_bear, dte, tp, sl, max_hold, show_trades=False):
    """Buy a call on each bear-regime oversold down-gap; simulate the bounce."""
    if df.empty or len(df) < ps.VOL_BASELINE_DAYS + 30 or ticker.upper() in ps.INVERSE_ETF_LIST:
        return []

    close = df["Close"]
    trades, last_entry = [], -COOLDOWN_DAYS - 1

    for t in range(ps.VOL_BASELINE_DAYS + 2, len(df) - 1):
        if t - last_entry < COOLDOWN_DAYS:
            continue
        # Only fire in a bear regime — the gate that turns this from a coin flip
        # into an edge. detect_drift(bearish_allowed=True) supplies the oversold
        # down-gap setup; we trade it LONG (a bounce), not short.
        if not (spy_bear and spy_bear.get(df.index[t].date(), False)):
            continue
        setup = detect_drift(df, as_of=t, bearish_allowed=True)
        if not setup or setup["direction"] != "bearish":
            continue

        spot = float(close.iloc[t])
        if spot <= 0:
            continue
        strike = spot                              # ATM call
        iv = _hist_vol(close, t)
        entry_date = df.index[t].date()
        expiry = entry_date + datetime.timedelta(days=dte)
        entry = _bs_price(spot, strike, dte / 365.0, iv, True) * (1 + SLIPPAGE_PCT)
        if entry <= 0.05:
            continue
        contracts = max(1, int(STARTING_CAPITAL * RISK_PER_TRADE_PCT // (entry * 100)))

        exit_p, reason = entry, "time"
        hold = min(max_hold, len(df) - 1 - t)
        for d in range(1, hold + 1):
            s_d = float(close.iloc[t + d])
            if not np.isfinite(s_d) or s_d <= 0:
                break
            days_left = (expiry - df.index[t + d].date()).days
            val = _bs_price(s_d, strike, max(0.0, days_left / 365.0), iv, True)
            ch = (val - entry) / entry
            if ch >= tp:
                exit_p, reason = val, "take_profit"; break
            if ch <= -sl:
                exit_p, reason = val, "stop_loss"; break
            exit_p = val

        pnl = round((exit_p - entry) * 100 * contracts
                    - COMMISSION_PER_CONTRACT * contracts * 2, 2)
        trades.append({"ticker": ticker, "entry_date": entry_date.isoformat(),
                       "reason": reason, "pnl": pnl, "conviction": setup["conviction"]})
        last_entry = t
        if show_trades:
            print(f"  {ticker:6s} {entry_date} bounce-call ${entry:.2f}->${exit_p:.2f} "
                  f"{'+' if pnl >= 0 else ''}{pnl:8.2f} {reason}")
    return trades


def _pf(trades):
    w = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    l = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return abs(w / l) if l != 0 else float("inf")


def run_backtest(tickers, days, dte, tp, sl, max_hold, show_trades=False):
    print("\n" + "=" * 72)
    print("  CAPITULATION-BOUNCE BACKTEST  —  Strategy 5 (bear-regime longs)")
    print(f"  Tickers: {len(tickers)} | Lookback: {days}d | DTE: {dte} | "
          f"hold {max_hold}d | TP +{tp:.0%} / SL -{sl:.0%}")
    spy_bear = _load_spy_regime(days)
    print(f"  Bear-regime days available: {sum(1 for v in spy_bear.values() if v)}")
    print("=" * 72)

    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    all_trades = []
    for idx, ticker in enumerate(tickers, 1):
        try:
            df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]]
        except Exception as e:
            print(f"  [{idx}/{len(tickers)}] {ticker}: download failed ({e})")
            continue
        t = backtest_ticker(ticker, df, spy_bear, dte, tp, sl, max_hold, show_trades)
        all_trades.extend(t)
        print(f"  [{idx}/{len(tickers)}] {ticker:6s} — {len(t)} bounce trades")

    print("\n" + "=" * 72 + "\n  SUMMARY")
    if not all_trades:
        print("  No trades (no bear-regime oversold gaps in this window).\n" + "=" * 72)
        return all_trades
    n = len(all_trades)
    pnl = sum(t["pnl"] for t in all_trades)
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    print(f"  Trades: {n} | Win rate: {wins / n * 100:.1f}% | PF: {_pf(all_trades):.2f} | "
          f"P&L: ${pnl:+.2f} | Return: {pnl / STARTING_CAPITAL * 100:+.1f}%")
    reasons = {}
    for t in all_trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print("  Exit reasons: " + " | ".join(f"{k}: {v}" for k, v in reasons.items()))
    print("=" * 72 + "\n")
    return all_trades


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Capitulation-bounce backtest — Strategy 5")
    p.add_argument("--tickers", nargs="+", default=DEFAULT_UNIVERSE)
    p.add_argument("--universe", action="store_true", help="Use the S&P 500 (Wikipedia)")
    p.add_argument("--max-tickers", type=int, default=None)
    p.add_argument("--days", type=int, default=1460)
    p.add_argument("--dte", type=int, default=DTE_ENTRY)
    p.add_argument("--tp", type=float, default=TAKE_PROFIT)
    p.add_argument("--sl", type=float, default=STOP_LOSS)
    p.add_argument("--hold", type=int, default=MAX_HOLD_DAYS)
    p.add_argument("--show-trades", action="store_true")
    a = p.parse_args()

    tickers = a.tickers
    if a.universe:
        from backtest_hft import fetch_sp500_tickers
        tickers = fetch_sp500_tickers(max_tickers=a.max_tickers or 90)
    elif a.max_tickers:
        tickers = tickers[:a.max_tickers]

    run_backtest(tickers, a.days, a.dte, a.tp, a.sl, a.hold, show_trades=a.show_trades)
