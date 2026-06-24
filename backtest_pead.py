"""
backtest_pead.py  —  Strategy 4: Post-Earnings / News-Drift Backtest

Validates the pead_scanner drift signal over years of daily history using
yfinance data (no Tradier — the free tier lacks multi-year history) and a
Black-Scholes option model so the multi-day hold pays realistic theta.

Methodology:
  1. Download daily OHLCV per ticker (yfinance).
  2. Walk forward day by day; at each day run the LIVE detector
     pead_scanner.detect_drift(df, as_of=t) — so the backtest validates the
     exact production logic.
  3. On a qualifying setup (and not in a per-ticker cooldown) enter an ATM
     option in the drift direction (call if bullish, put if bearish), priced
     with Black-Scholes using the stock's historical vol as the IV proxy.
  4. Simulate forward, repricing the option each day as spot moves and time
     decays, and exit on take-profit / stop-loss / drift-fade (gap fill) /
     min-DTE / max-hold.
  5. Aggregate P&L, win rate, profit factor — broken down by direction and
     conviction.

Run:
    python backtest_pead.py
    python backtest_pead.py --days 1095 --tickers AAPL NVDA TSLA
    python backtest_pead.py --tp 0.6 --sl 0.35 --max-hold 10 --show-trades
"""

import argparse
import datetime
import math

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import pead_scanner as ps
from pead_scanner import detect_drift

load_dotenv()


# ─── Config ──────────────────────────────────────────────────────────────────

STARTING_CAPITAL   = 10_000.0
RISK_PER_TRADE_PCT = 0.02      # 2% of equity per trade (swing sizing)
LOOKBACK_DAYS      = 730       # ~2 years of daily history

DTE_ENTRY          = 21        # calendar days to expiry at entry
TAKE_PROFIT_PCT    = 0.80      # exit at +80% on the option (validated)
STOP_LOSS_PCT      = 0.35      # exit at -35% on the option
MAX_HOLD_DAYS      = 12        # trading-day time stop
MIN_DTE_EXIT       = 3         # bail when this few calendar days remain
COOLDOWN_DAYS      = 5         # don't re-enter the same name for N trading days

COMMISSION_PER_CONTRACT = 0.65
SLIPPAGE_PCT            = 0.01  # 1% on entry (swing names are liquid vs 0-DTE)

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
    "NFLX", "MU", "AVGO", "PLTR", "QCOM", "ADBE", "CRM", "PYPL",
    "UBER", "SHOP", "PANW", "CRWD", "SNOW", "MRVL", "ANET", "COIN",
    "NET", "DDOG", "TTD", "RBLX", "HOOD", "ABNB",
]


# ─── Black-Scholes (ATM call/put) ────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, iv: float, is_call: bool) -> float:
    """Black-Scholes price (r=0). Intrinsic value at/after expiry."""
    if T <= 0 or iv <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if is_call:
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _hist_vol(close: pd.Series, end_idx: int, window: int = 20) -> float:
    """Annualised historical volatility from daily returns before end_idx."""
    rets = close.pct_change(fill_method=None).iloc[max(0, end_idx - window):end_idx]
    if len(rets) < 5:
        return 0.40
    daily = float(rets.std())
    return max(0.10, daily * math.sqrt(252))


# ─── Single-Ticker Backtest ──────────────────────────────────────────────────

def backtest_ticker(ticker: str, df: pd.DataFrame, tp: float, sl: float,
                    max_hold: int, spy_bear: dict | None = None,
                    show_trades: bool = False) -> list[dict]:
    """Replay detect_drift over df and simulate each resulting option trade.

    spy_bear maps a date -> True when SPY was below its 200-day SMA that day,
    so the bearish/put side is gated to bear-regime dates exactly as live.
    """
    if df.empty or len(df) < ps.VOL_BASELINE_DAYS + 30:
        return []
    if ticker.upper() in ps.INVERSE_ETF_LIST:
        return []

    close = df["Close"]
    trades: list[dict] = []
    last_entry_idx = -COOLDOWN_DAYS - 1

    # Leave room for the forward hold simulation.
    for t in range(ps.VOL_BASELINE_DAYS + 2, len(df) - 1):
        if t - last_entry_idx < COOLDOWN_DAYS:
            continue

        bearish_allowed = bool(spy_bear.get(df.index[t].date(), False)) if spy_bear else False
        setup = detect_drift(df, as_of=t, bearish_allowed=bearish_allowed)
        if not setup:
            continue

        is_call = setup["direction"] == "bullish"
        spot = float(close.iloc[t])
        if spot <= 0:
            continue
        strike = spot                      # ATM
        iv = _hist_vol(close, t)
        entry_date = df.index[t].date()
        expiry_date = entry_date + datetime.timedelta(days=DTE_ENTRY)

        entry_prem = _bs_price(spot, strike, DTE_ENTRY / 365.0, iv, is_call)
        if entry_prem <= 0.05:
            continue

        # Position sizing (compounding equity handled by the caller via PnL sum;
        # here use the running notion of $ at risk per trade on starting capital).
        budget = STARTING_CAPITAL * RISK_PER_TRADE_PCT
        contracts = max(1, int(budget // (entry_prem * 100)))

        exit_prem = entry_prem
        exit_reason = "time"
        prev_close_level = setup["prev_close"]   # pre-event price = gap-fill level

        hold = min(max_hold, len(df) - 1 - t)
        for d in range(1, hold + 1):
            s_d = float(close.iloc[t + d])
            if not np.isfinite(s_d) or s_d <= 0:
                break   # skip NaN/zero data bars rather than poisoning P&L
            days_left = (expiry_date - df.index[t + d].date()).days
            t_years = max(0.0, days_left / 365.0)
            val = _bs_price(s_d, strike, t_years, iv, is_call)
            pnl_pct = (val - entry_prem) / entry_prem

            if pnl_pct >= tp:
                exit_prem, exit_reason = val, "take_profit"
                break
            if pnl_pct <= -sl:
                exit_prem, exit_reason = val, "stop_loss"
                break
            # Drift fade: the move has fully retraced to the pre-event level.
            if (is_call and s_d <= prev_close_level) or ((not is_call) and s_d >= prev_close_level):
                exit_prem, exit_reason = val, "drift_fade"
                break
            if days_left <= MIN_DTE_EXIT:
                exit_prem, exit_reason = val, "dte"
                break
            exit_prem = val

        gross = (exit_prem - entry_prem) * contracts * 100
        costs = (COMMISSION_PER_CONTRACT * contracts * 2
                 + entry_prem * 100 * contracts * SLIPPAGE_PCT)
        pnl = round(gross - costs, 2)

        trades.append({
            "ticker": ticker, "direction": setup["direction"],
            "conviction": setup["conviction"], "score": setup["setup_score"],
            "trend_aligned": setup["trend_aligned"], "held_frac": setup["held_frac"],
            "entry_date": entry_date.isoformat(), "exit_reason": exit_reason,
            "event_move": setup["event_move"], "move_z": setup["move_z"],
            "entry_prem": round(entry_prem, 2), "exit_prem": round(exit_prem, 2),
            "contracts": contracts, "pnl": pnl,
        })
        last_entry_idx = t

        if show_trades:
            print(f"  {ticker:6s} {entry_date} {setup['direction']:8s} "
                  f"z={setup['move_z']:+.1f} score={setup['setup_score']:3d} "
                  f"prem ${entry_prem:.2f}->${exit_prem:.2f} "
                  f"{'+' if pnl >= 0 else ''}{pnl:8.2f} {exit_reason}")

    return trades


# ─── Market-Regime Series (gates the bearish side) ───────────────────────────

def _load_spy_regime(days: int) -> dict:
    """
    Map date -> True when SPY closed below its 200-day SMA (a bear regime).

    Pulls SPY with extra history so the 200-day SMA is valid from the start of
    the backtest window. Returns {} (treated as all-bull / long-only) on failure.
    """
    start = (datetime.date.today() - datetime.timedelta(days=days + 320)).isoformat()
    try:
        spy = yf.download(ps.REGIME_TICKER, start=start, progress=False, auto_adjust=True)
        if spy.empty:
            return {}
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        close = spy["Close"]
        sma = close.rolling(ps.REGIME_SMA_DAYS).mean()
        bear = close < sma          # NaN SMA (first 200 bars) compares False
        return {idx.date(): bool(v) for idx, v in bear.items()}
    except Exception as e:
        print(f"  SPY regime download failed ({e}) — backtest will be long-only")
        return {}


# ─── Multi-Ticker Backtest ───────────────────────────────────────────────────

def run_backtest(tickers: list[str], days: int, tp: float, sl: float,
                 max_hold: int, show_trades: bool = False) -> list[dict]:
    print("\n" + "=" * 72)
    print("  PEAD / NEWS-DRIFT BACKTEST  —  Strategy 4")
    print(f"  Tickers: {len(tickers)}  |  Lookback: {days}d  |  "
          f"DTE: {DTE_ENTRY}  |  TP +{tp:.0%} / SL -{sl:.0%} / hold {max_hold}d")

    # Regime gate for the bearish side (mirrors live BEARISH_REGIME_FILTER).
    spy_bear = _load_spy_regime(days) if ps.BEARISH_REGIME_FILTER else {}
    bear_days = sum(1 for v in spy_bear.values() if v)
    print(f"  Bearish gate: {'REGIME (SPY<200dSMA)' if ps.BEARISH_REGIME_FILTER else 'OFF (long-only)'}"
          f"  |  bear-regime days available: {bear_days}")
    print("=" * 72)

    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    all_trades: list[dict] = []

    for idx, ticker in enumerate(tickers, 1):
        try:
            df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if df.empty:
                continue
            # Flatten possible MultiIndex columns from yfinance.
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]]
        except Exception as e:
            print(f"  [{idx}/{len(tickers)}] {ticker}: download failed ({e})")
            continue

        t = backtest_ticker(ticker, df, tp, sl, max_hold, spy_bear=spy_bear,
                            show_trades=show_trades)
        all_trades.extend(t)
        print(f"  [{idx}/{len(tickers)}] {ticker:6s} — {len(t)} trades")

    _summary(all_trades, tp, sl, max_hold)
    return all_trades


def _pf(trades: list[dict]) -> float:
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return abs(wins / losses) if losses != 0 else float("inf")


def _summary(trades: list[dict], tp: float, sl: float, max_hold: int) -> None:
    print("\n" + "=" * 72)
    print("  PEAD BACKTEST SUMMARY")
    print("=" * 72)
    if not trades:
        print("  No trades triggered.")
        return

    n = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / n * 100
    pf = _pf(trades)
    ret = pnl / STARTING_CAPITAL * 100

    print(f"\n  Trades: {n}  |  Win rate: {win_rate:.1f}%  |  "
          f"PF: {pf:.2f}  |  P&L: ${pnl:+.2f}  |  Return: {ret:+.1f}%")

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print("  Exit reasons: " + " | ".join(f"{k}: {v}" for k, v in reasons.items()))

    print("\n  By direction:")
    for d in ("bullish", "bearish"):
        sub = [t for t in trades if t["direction"] == d]
        if sub:
            w = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
            print(f"    {d:8s} n={len(sub):4d} | win {w:4.0f}% | "
                  f"PF {_pf(sub):.2f} | P&L ${sum(t['pnl'] for t in sub):+.2f}")

    print("\n  By conviction:")
    for c in ("high", "relaxed"):
        sub = [t for t in trades if t["conviction"] == c]
        if sub:
            w = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
            print(f"    {c:8s} n={len(sub):4d} | win {w:4.0f}% | "
                  f"PF {_pf(sub):.2f} | P&L ${sum(t['pnl'] for t in sub):+.2f}")
    print("\n" + "=" * 72 + "\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PEAD / news-drift backtest — Strategy 4")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_UNIVERSE)
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--tp", type=float, default=TAKE_PROFIT_PCT)
    parser.add_argument("--sl", type=float, default=STOP_LOSS_PCT)
    parser.add_argument("--max-hold", type=int, default=MAX_HOLD_DAYS)
    parser.add_argument("--show-trades", action="store_true")
    parser.add_argument("--long-only", action="store_true",
                        help="Disable the regime-gated bearish side (A/B baseline)")
    args = parser.parse_args()

    if args.long_only:
        ps.BEARISH_REGIME_FILTER = False

    run_backtest(args.tickers, args.days, args.tp, args.sl, args.max_hold,
                 show_trades=args.show_trades)
