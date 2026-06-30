"""
backtest_bear_call.py — bear-call CREDIT spread for bearish drift (the disciplined
retry of the PEAD short side).

Buying long PUTS to express post-gap bearish drift FAILED (8% win, big loss):
puts are expensive in high-IV bear markets and bleed theta / IV-crush. A
BEAR-CALL spread flips that — SELL a near-OTM call, BUY a further-OTM call for a
net CREDIT — so you COLLECT premium and theta and carry DEFINED risk. That's the
structure that should survive bear-market snapback rallies far better than long
puts.

This uses the SAME signal as the rejected PEAD short (a down-gap drift in a bear
regime, via pead_scanner.detect_drift with bearish_allowed=True), so it's a clean
A/B against the long-put result (-$12.7k). Daily yfinance data + Black-Scholes leg
pricing are reused from backtest_pead so the multi-day hold pays realistic theta.

Run:
    python backtest_bear_call.py --days 1460          # 4 years incl. the 2022 bear
    python backtest_bear_call.py --tp 0.5 --sl 2.0 --max-hold 12 --show-trades
"""

import argparse
import datetime

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import mawitek.strategies.pead_scanner as ps
from mawitek.strategies.pead_scanner import detect_drift
from backtests.backtest_pead import (
    _bs_price, _hist_vol, _load_spy_regime, DEFAULT_UNIVERSE,
    STARTING_CAPITAL, RISK_PER_TRADE_PCT, COMMISSION_PER_CONTRACT,
)

load_dotenv()


# ─── Config ──────────────────────────────────────────────────────────────────

DTE_ENTRY        = 30        # calendar days — credit spreads want a little room
SHORT_OTM        = 0.04      # short call ~4% above spot (the level we defend)
LONG_OTM         = 0.09      # long call ~9% above spot (caps the risk)
TAKE_PROFIT_FRAC = 0.50      # buy back once 50% of the credit is captured
STOP_LOSS_MULT   = 2.0       # stop if cost-to-close reaches 2x the credit (−1x credit)
MAX_HOLD_DAYS    = 12
MIN_DTE_EXIT     = 3
COOLDOWN_DAYS    = 5
SLIPPAGE_PER_LEG = 0.02      # 2% of each leg's mid, given up on entry


def _spread_cost(S: float, Ks: float, Kl: float, T: float, iv: float) -> float:
    """Cost to close a bear-call spread = short-call value − long-call value (>=0)."""
    return max(0.0, _bs_price(S, Ks, T, iv, True) - _bs_price(S, Kl, T, iv, True))


# ─── Single-Ticker Backtest ──────────────────────────────────────────────────

def backtest_ticker(ticker, df, spy_bear, tp, sl, max_hold, show_trades=False):
    if df.empty or len(df) < ps.VOL_BASELINE_DAYS + 30 or ticker.upper() in ps.INVERSE_ETF_LIST:
        return []

    close = df["Close"]
    trades, last_entry = [], -COOLDOWN_DAYS - 1

    for t in range(ps.VOL_BASELINE_DAYS + 2, len(df) - 1):
        if t - last_entry < COOLDOWN_DAYS:
            continue

        # Same trigger as the PEAD short: a bearish drift setup, regime-gated.
        bear = bool(spy_bear.get(df.index[t].date(), False)) if spy_bear else False
        setup = detect_drift(df, as_of=t, bearish_allowed=bear)
        if not setup or setup["direction"] != "bearish":
            continue

        spot = float(close.iloc[t])
        if spot <= 0:
            continue
        Ks, Kl = spot * (1 + SHORT_OTM), spot * (1 + LONG_OTM)
        iv = _hist_vol(close, t)
        entry_date = df.index[t].date()
        expiry = entry_date + datetime.timedelta(days=DTE_ENTRY)
        t0 = DTE_ENTRY / 365.0

        gross_credit = _spread_cost(spot, Ks, Kl, t0, iv)
        if gross_credit <= 0.05:
            continue
        # Entry slippage: we receive slightly less than mid credit.
        slip = SLIPPAGE_PER_LEG * (_bs_price(spot, Ks, t0, iv, True) + _bs_price(spot, Kl, t0, iv, True))
        credit = gross_credit - slip
        if credit <= 0:
            continue

        max_loss = (Kl - Ks) - credit            # per share; defined risk
        if max_loss <= 0:
            continue
        budget = STARTING_CAPITAL * RISK_PER_TRADE_PCT
        contracts = max(1, int(budget // (max_loss * 100)))

        exit_val, reason = credit, "expiry"
        hold = min(max_hold, len(df) - 1 - t)
        for d in range(1, hold + 1):
            s_d = float(close.iloc[t + d])
            if not np.isfinite(s_d) or s_d <= 0:
                break
            days_left = (expiry - df.index[t + d].date()).days
            val = _spread_cost(s_d, Ks, Kl, max(0.0, days_left / 365.0), iv)
            if val <= credit * (1 - tp):          # captured `tp` of the credit
                exit_val, reason = val, "take_profit"
                break
            if val >= credit * sl:                # spread ran against us
                exit_val, reason = val, "stop_loss"
                break
            if days_left <= MIN_DTE_EXIT:
                exit_val, reason = val, "dte"
                break
            exit_val = val

        # P&L = credit received − cost to close, minus commissions on 4 legs total.
        pnl = round((credit - exit_val) * 100 * contracts
                    - COMMISSION_PER_CONTRACT * contracts * 4, 2)
        trades.append({
            "ticker": ticker, "entry_date": entry_date.isoformat(), "reason": reason,
            "credit": round(credit, 2), "exit_val": round(exit_val, 2),
            "contracts": contracts, "pnl": pnl, "conviction": setup["conviction"],
        })
        last_entry = t
        if show_trades:
            print(f"  {ticker:6s} {entry_date} bear-call | credit ${credit:.2f} -> close ${exit_val:.2f}"
                  f" | {'+' if pnl >= 0 else ''}{pnl:8.2f} {reason}")
    return trades


# ─── Multi-Ticker Backtest ───────────────────────────────────────────────────

def _pf(trades):
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return abs(wins / loss) if loss != 0 else float("inf")


def run_backtest(tickers, days, tp, sl, max_hold, show_trades=False):
    print("\n" + "=" * 72)
    print("  BEAR-CALL CREDIT-SPREAD BACKTEST  (bearish drift, defined risk)")
    print(f"  Tickers: {len(tickers)} | Lookback: {days}d | DTE: {DTE_ENTRY} | "
          f"short +{SHORT_OTM:.0%}/long +{LONG_OTM:.0%} | TP {tp:.0%} credit / SL {sl:.1f}x")

    # Bear-call spreads are a bear-regime play, so always gate on the SPY regime.
    spy_bear = _load_spy_regime(days)
    bear_days = sum(1 for v in spy_bear.values() if v)
    print(f"  Bear-regime days available: {bear_days}")
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
        t = backtest_ticker(ticker, df, spy_bear, tp, sl, max_hold, show_trades)
        all_trades.extend(t)
        print(f"  [{idx}/{len(tickers)}] {ticker:6s} — {len(t)} bear-call trades")

    print("\n" + "=" * 72 + "\n  SUMMARY")
    if not all_trades:
        print("  No trades (no bearish drift in a bear regime over this window).\n" + "=" * 72)
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
    print("  (Compare: the long-PUT version of this exact signal lost ~-$12.7k at 8% win.)")
    print("=" * 72 + "\n")
    return all_trades


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Bear-call credit-spread backtest")
    p.add_argument("--tickers", nargs="+", default=DEFAULT_UNIVERSE)
    p.add_argument("--days", type=int, default=1460)
    p.add_argument("--tp", type=float, default=TAKE_PROFIT_FRAC)
    p.add_argument("--sl", type=float, default=STOP_LOSS_MULT)
    p.add_argument("--max-hold", type=int, default=MAX_HOLD_DAYS)
    p.add_argument("--show-trades", action="store_true")
    a = p.parse_args()
    run_backtest(a.tickers, a.days, a.tp, a.sl, a.max_hold, show_trades=a.show_trades)
