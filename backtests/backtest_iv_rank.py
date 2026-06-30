"""
backtest_iv_rank.py  -  Strategy 2: IV Rank Backtest

Backtests the HV-rank premium selling/buying strategy over a configurable
lookback window using only yfinance daily data (no Tradier required).

Methodology:
  1. Download daily prices per ticker (yfinance)
  2. Compute rolling HV30 as the IV proxy (same as iv_rank_bot.py)
  3. Weekly scan: IVR >= 75 -> bull-put credit spread (sell premium)
                  IVR <= 25 -> long straddle (buy premium)
  4. Price legs with Black-Scholes using HV30 as the vol input
  5. Simulate P&L at expiry using actual stock returns
  6. Track equity curve, win rate, drawdown

Run:
    python backtest_iv_rank.py
    python backtest_iv_rank.py --days 365
    python backtest_iv_rank.py --tickers AAPL NVDA MSFT --dte 21
"""

import argparse
import datetime
import json
import math

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# --- Config -------------------------------------------------------------------

STARTING_CAPITAL   = 50_000
RISK_PER_TRADE_PCT = 0.02       # 2% per trade (matches live bot)
LOOKBACK_DAYS      = 730        # 2 years
SCAN_INTERVAL_DAYS = 7          # Weekly scan cadence
DTE_TARGET         = 30         # Hold period in calendar days

IVR_SELL_THRESHOLD   = 75       # IVR >= 75: sell premium (put spread)
IVR_BUY_THRESHOLD   = 15        # IVR <= 15: buy premium (straddle) — tighter than before
MIN_SETUP_SCORE      = 50
MIN_HV               = 0.15     # Skip near-zero-vol environments
MIN_HV_FOR_STRADDLE  = 0.25    # Straddle only when HV >= 25% (stock must move enough to cover premium)

# Bull-put spread: sell put 5% OTM, buy put 10% OTM
SELL_PUT_OTM = 0.95
BUY_PUT_OTM  = 0.90

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
    "NFLX", "MU", "AVGO", "PLTR", "QCOM", "ADBE", "CRM", "PYPL",
    "UBER", "SHOP", "PANW", "CRWD", "SNOW", "MRVL", "ANET", "COIN",
    "NET", "DDOG", "TTD", "RBLX", "HOOD", "ABNB",
]


# --- Black-Scholes -----------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_put(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return max(0.0, K - S)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_call(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return max(0.0, S - K)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


# --- IV Rank (HV30 proxy) ----------------------------------------------------

def _compute_hv_rank_series(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """
    Returns (hv30_series, iv_rank_series) aligned to the close index.
    HV rank = where today's HV30 sits in the 52-week rolling HV range.
    Mirrors compute_iv_rank() in iv_rank_bot.py but vectorised.
    """
    log_ret = np.log(close / close.shift(1))
    hv30    = log_ret.rolling(30).std() * math.sqrt(252)

    hv_52w_low  = hv30.rolling(252).min()
    hv_52w_high = hv30.rolling(252).max()
    iv_range    = hv_52w_high - hv_52w_low

    rank = (hv30 - hv_52w_low) / iv_range.replace(0.0, np.nan) * 100.0
    rank = rank.clip(0.0, 100.0)

    return hv30, rank


def _score(iv_rank: float, hv: float, signal: str) -> int:
    """Port of iv_rank_bot.score_iv_setup() using HV as IV proxy."""
    score = 0
    if signal == "sell_premium":
        if iv_rank >= 95:   score += 50
        elif iv_rank >= 90: score += 40
        elif iv_rank >= 85: score += 30
        else:               score += 20   # >= 75

        if hv >= 0.80:      score += 30
        elif hv >= 0.60:    score += 25
        elif hv >= 0.40:    score += 20
        elif hv >= 0.30:    score += 10

        score += 20   # static DTE-availability bonus

    elif signal == "buy_premium":
        if iv_rank <= 5:    score += 50
        elif iv_rank <= 10: score += 40
        elif iv_rank <= 15: score += 30
        else:               score += 20   # <= 25

        if hv <= 0.15:      score += 30
        elif hv <= 0.20:    score += 25
        elif hv <= 0.25:    score += 20
        elif hv <= 0.30:    score += 10

        score += 20

    return min(score, 100)


# --- P&L models --------------------------------------------------------------

def _bull_put_pnl(
    entry: float, exit_px: float, hv: float, dte: int, budget: float
) -> dict:
    """
    Bull-put credit spread held to expiry.
    Sell put at SELL_PUT_OTM*entry, buy put at BUY_PUT_OTM*entry.
    Uses Black-Scholes with hv as IV to price at entry.
    """
    T = dte / 365.0
    sell_k = entry * SELL_PUT_OTM
    buy_k  = entry * BUY_PUT_OTM

    sell_px = _bs_put(entry, sell_k, T, hv)
    buy_px  = _bs_put(entry, buy_k,  T, hv)
    credit  = sell_px - buy_px

    if credit <= 0.0:
        return {"pnl": 0.0, "contracts": 0, "reason": "no_credit"}

    max_profit_per = credit * 100
    max_loss_per   = (sell_k - buy_k - credit) * 100

    if max_loss_per <= 0.0:
        return {"pnl": 0.0, "contracts": 0, "reason": "invalid_spread"}

    contracts = max(1, int(budget / max_loss_per))

    if exit_px >= sell_k:
        pnl_per = max_profit_per
        reason  = "full_profit"
    elif exit_px <= buy_k:
        pnl_per = -max_loss_per
        reason  = "max_loss"
    else:
        frac    = (exit_px - buy_k) / (sell_k - buy_k)
        pnl_per = -max_loss_per + frac * (max_profit_per + max_loss_per)
        reason  = "partial"

    return {
        "pnl":       round(pnl_per * contracts, 2),
        "contracts": contracts,
        "credit":    round(credit, 4),
        "reason":    reason,
    }


def _straddle_pnl(
    entry: float, exit_px: float, hv: float, dte: int, budget: float
) -> dict:
    """
    Long straddle (ATM call + ATM put) held to expiry.
    Priced with Black-Scholes at entry; valued at intrinsic at expiry.
    """
    T    = dte / 365.0
    call = _bs_call(entry, entry, T, hv)
    put  = _bs_put(entry,  entry, T, hv)
    debit = call + put   # per share

    if debit <= 0.0:
        return {"pnl": 0.0, "contracts": 0, "reason": "no_debit"}

    cost_per_contract = debit * 100
    contracts = max(1, int(budget / cost_per_contract))

    intrinsic = abs(exit_px - entry)
    pnl_per   = (intrinsic - debit) * 100

    return {
        "pnl":       round(pnl_per * contracts, 2),
        "contracts": contracts,
        "debit":     round(debit, 4),
        "reason":    "profit" if pnl_per > 0 else "loss",
    }


# --- Main backtest -----------------------------------------------------------

def run_backtest(
    tickers: list[str] = UNIVERSE,
    lookback_days: int  = LOOKBACK_DAYS,
    scan_interval: int  = SCAN_INTERVAL_DAYS,
    dte: int            = DTE_TARGET,
    starting_capital: float = STARTING_CAPITAL,
) -> dict:

    print("\n" + "=" * 62)
    print("  IV RANK BACKTEST  -  Strategy 2")
    print(f"  Universe: {len(tickers)} stocks | Lookback: {lookback_days}d")
    print(f"  Capital: ${starting_capital:,.0f} | Risk/trade: {RISK_PER_TRADE_PCT:.0%}")
    print(f"  DTE: {dte} | Scan interval: {scan_interval}d")
    print("=" * 62 + "\n")

    equity       = starting_capital
    trades: list[dict] = []
    equity_curve: list[dict] = []

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=lookback_days)
    # Download extra history so HV rank has a full 52-week baseline
    dl_start = cutoff - datetime.timedelta(days=400)

    for ticker in tickers:
        print(f"[{ticker}] downloading...", end=" ", flush=True)

        try:
            raw = yf.download(
                ticker,
                start=dl_start.strftime("%Y-%m-%d"),
                end=today.strftime("%Y-%m-%d"),
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]
            close = raw["Close"].dropna().astype(float)
        except Exception as e:
            print(f"error: {e}")
            continue

        if len(close) < 300:
            print("skipped (insufficient data)")
            continue

        hv30, iv_rank = _compute_hv_rank_series(close)

        # Convert index to plain dates for comparison
        idx_dates = pd.Series(close.index, index=close.index).apply(
            lambda t: t.date() if hasattr(t, "date") else datetime.datetime.strptime(str(t)[:10], "%Y-%m-%d").date()
        )

        ticker_trades = 0
        scan_date     = cutoff

        while scan_date <= today - datetime.timedelta(days=dte + 1):
            # Find the last bar on or before scan_date
            valid = idx_dates[idx_dates <= scan_date]
            if valid.empty:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            bar_idx = valid.index[-1]

            hv_val  = hv30.get(bar_idx)
            ivr_val = iv_rank.get(bar_idx)

            if hv_val is None or ivr_val is None or np.isnan(hv_val) or np.isnan(ivr_val):
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            if hv_val < MIN_HV:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            # Signal
            if ivr_val >= IVR_SELL_THRESHOLD:
                signal = "sell_premium"
            elif ivr_val <= IVR_BUY_THRESHOLD:
                # Skip straddle if HV is too low — premium won't be covered by the move
                if hv_val < MIN_HV_FOR_STRADDLE:
                    scan_date += datetime.timedelta(days=scan_interval)
                    continue
                signal = "buy_premium"
            else:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            score = _score(ivr_val, hv_val, signal)
            if score < MIN_SETUP_SCORE:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            entry_price = float(close.get(bar_idx, 0))
            if entry_price <= 0:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            # Exit: first bar on or after (scan_date + dte)
            exit_target = scan_date + datetime.timedelta(days=dte)
            valid_exit  = idx_dates[idx_dates >= exit_target]
            if valid_exit.empty:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            exit_bar_idx = valid_exit.index[0]
            exit_price   = float(close.get(exit_bar_idx, 0))
            if exit_price <= 0:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            budget = equity * RISK_PER_TRADE_PCT

            if signal == "sell_premium":
                result = _bull_put_pnl(entry_price, exit_price, hv_val, dte, budget)
            else:
                result = _straddle_pnl(entry_price, exit_price, hv_val, dte, budget)

            if result["contracts"] == 0:
                scan_date += datetime.timedelta(days=scan_interval)
                continue

            pnl    = result["pnl"]
            equity += pnl
            ticker_trades += 1

            entry_date_str = idx_dates.get(bar_idx, scan_date).isoformat() if hasattr(idx_dates.get(bar_idx, scan_date), 'isoformat') else str(idx_dates.get(bar_idx, scan_date))
            exit_date_str  = idx_dates.get(exit_bar_idx, exit_target).isoformat() if hasattr(idx_dates.get(exit_bar_idx, exit_target), 'isoformat') else str(idx_dates.get(exit_bar_idx, exit_target))

            stock_move = (exit_price - entry_price) / entry_price * 100

            print(
                f"\n  {'WIN' if pnl > 0 else 'LOSS'}  {ticker} {entry_date_str}"
                f" | {signal[:4].upper()} | IVR: {ivr_val:.0f}"
                f" | HV: {hv_val*100:.0f}% | Move: {stock_move:+.1f}%"
                f" | P&L: ${pnl:+.0f} ({result['reason']})"
            )

            trades.append({
                "ticker":       ticker,
                "signal":       signal,
                "score":        score,
                "entry_date":   entry_date_str,
                "exit_date":    exit_date_str,
                "entry_price":  round(entry_price, 2),
                "exit_price":   round(exit_price, 2),
                "stock_move_pct": round(stock_move, 2),
                "iv_rank":      round(ivr_val, 1),
                "hv30_pct":     round(hv_val * 100, 1),
                "contracts":    result["contracts"],
                "pnl":          round(pnl, 2),
                "reason":       result["reason"],
                "equity_after": round(equity, 2),
                "win":          pnl > 0,
            })

            equity_curve.append({
                "date":   exit_date_str,
                "equity": round(equity, 2),
            })

            # Don't re-enter this ticker until current trade expires
            scan_date = exit_target + datetime.timedelta(days=1)

        if ticker_trades == 0:
            print("no signals")
        else:
            print(f"\n  [{ticker}] {ticker_trades} trades")

    # --- Summary ----------------------------------------------------------------

    if not trades:
        print("\n[Backtest] No trades generated.")
        return {}

    df = pd.DataFrame(trades)
    total  = len(df)
    wins   = int(df["win"].sum())
    losses = total - wins
    wr     = round(wins / total * 100, 1)

    avg_pnl  = round(float(df["pnl"].mean()), 2)
    avg_win  = round(float(df[df["win"]]["pnl"].mean()), 2)  if wins   else 0.0
    avg_loss = round(float(df[~df["win"]]["pnl"].mean()), 2) if losses else 0.0

    total_pnl = round(float(df["pnl"].sum()), 2)
    total_ret = round((equity - starting_capital) / starting_capital * 100, 1)

    vals   = [starting_capital] + list(df["equity_after"])
    peak   = starting_capital
    max_dd = 0.0
    for v in vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd
    max_dd = round(max_dd, 1)

    signals_breakdown = df["signal"].value_counts().to_dict()
    reasons_breakdown = df["reason"].value_counts().to_dict()

    summary = {
        "total_trades":     total,
        "wins":             wins,
        "losses":           losses,
        "win_rate":         wr,
        "avg_pnl":          avg_pnl,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "total_pnl":        total_pnl,
        "total_return_pct": total_ret,
        "max_drawdown_pct": max_dd,
        "final_equity":     round(equity, 2),
        "starting_capital": starting_capital,
        "signals":          signals_breakdown,
        "exit_reasons":     reasons_breakdown,
        "period_days":      lookback_days,
    }

    print("\n" + "=" * 62)
    print("  IV RANK BACKTEST RESULTS")
    print("=" * 62)
    print(f"  Period:       Last {lookback_days} days (~{lookback_days//365} years)")
    print(f"  Universe:     {len(tickers)} stocks")
    print(f"  Total trades: {total}")
    print(f"  Win rate:     {wr}%  ({wins}W / {losses}L)")
    print(f"  Avg P&L:      ${avg_pnl:+,.2f} per trade")
    print(f"  Avg win:      ${avg_win:+,.2f}")
    print(f"  Avg loss:     ${avg_loss:+,.2f}")
    print(f"  Total P&L:    ${total_pnl:+,.2f}")
    print(f"  Total return: {total_ret:+.1f}%")
    print(f"  Max drawdown: {max_dd:.1f}%")
    print(f"  Final equity: ${equity:,.2f}")
    print(f"  Signals:      {signals_breakdown}")
    print(f"  Exit reasons: {reasons_breakdown}")
    print("=" * 62 + "\n")

    df.to_csv("backtest_iv_rank.csv", index=False)
    print("[Backtest] Trade log saved -> backtest_iv_rank.csv")

    equity_curve_sorted = sorted(equity_curve, key=lambda x: x["date"])
    with open("backtest_iv_rank.json", "w") as f:
        json.dump({
            "summary":      summary,
            "equity_curve": equity_curve_sorted,
            "trades":       trades,
        }, f, indent=2)
    print("[Backtest] Equity curve saved -> backtest_iv_rank.json\n")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IV Rank Strategy 2 Backtest")
    parser.add_argument("--tickers", nargs="+", default=UNIVERSE)
    parser.add_argument("--days",    type=int, default=LOOKBACK_DAYS,
                        help=f"Lookback in calendar days (default {LOOKBACK_DAYS})")
    parser.add_argument("--dte",     type=int, default=DTE_TARGET,
                        help=f"Hold period in days (default {DTE_TARGET})")
    args = parser.parse_args()

    run_backtest(tickers=args.tickers, lookback_days=args.days, dte=args.dte)
