"""
backtest_orb.py  -  Strategy 4: Opening Range Breakout (ORB)

Methodology:
  1. Download 5-minute bars per ticker (yfinance, last 60 days max)
  2. Define the Opening Range from the first ORB_MINUTES of each session
  3. Wait for price to close above ORB high with a volume spike
  4. Buy a near-term ATM call (Black-Scholes priced, DTE=7)
  5. Exit on whichever triggers first:
       - Take profit  (+80% option gain)
       - Stop loss    (-40% option loss)
       - RSI >= 70    (overbought exhaustion)
       - Volume fade  (bar volume < 50% of entry bar volume, while in profit)
       - End of day   (time stop)

Filters applied before entry:
  - Gap-up filter  : today's open > prev close (momentum/news day)
  - ORB width      : range must be >= 0.3% of price (real conviction)
  - Entry window   : only look for breakouts in the first 2 hours after ORB

Run:
    python backtest_orb.py
    python backtest_orb.py --days 60 --tickers AAPL NVDA TSLA
    python backtest_orb.py --orb-minutes 5
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
RISK_PER_TRADE_PCT = 0.02       # 2% risk per trade
LOOKBACK_DAYS      = 60         # yfinance 5m limit

ORB_MINUTES        = 15         # Opening range window (5, 10, or 15)
VOLUME_SPIKE_MULT  = 1.5        # Breakout bar volume must exceed ORB avg vol x this
MAX_ENTRY_BARS     = 24         # Scan at most 24 bars after ORB (~2 hrs) for breakout
MIN_ORB_WIDTH_PCT  = 0.003      # Skip if ORB range < 0.3% of price (no conviction)

TAKE_PROFIT_PCT       = 0.80    # +80% on option premium
STOP_LOSS_PCT         = -0.25   # -25% on option premium (tightened to cut losers faster)
RSI_PERIOD            = 14
RSI_OVERBOUGHT        = 70
VOLUME_FADE_RATIO     = 0.50    # Exit if bar vol < 50% of entry vol (when profitable)
MIN_PROFIT_FOR_VOL_FADE = 0.15  # Only exit on vol fade if option is up >= 15% (filters micro-wins)
DTE_DAYS           = 7          # Near-term weekly option at entry
HV_WINDOW          = 20         # Days for historical vol estimate

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
    "NFLX", "MU", "AVGO", "PLTR", "QCOM", "ADBE", "CRM", "PYPL",
    "UBER", "SHOP", "PANW", "CRWD", "SNOW", "MRVL", "ANET", "COIN",
    "NET", "DDOG", "TTD", "RBLX", "HOOD", "ABNB",
]


# --- Black-Scholes ------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return max(0.01, S - K)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT
    return max(0.01, S * _norm_cdf(d1) - K * _norm_cdf(d2))


def _bs_delta(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return 1.0 if S > K else 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrtT)
    return _norm_cdf(d1)


def _bs_gamma(S: float, K: float, T: float, iv: float) -> float:
    if T <= 0 or iv <= 0 or S <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv ** 2 * T) / (iv * sqrtT)
    phi = math.exp(-0.5 * d1 ** 2) / math.sqrt(2.0 * math.pi)
    return phi / (S * iv * sqrtT)


# --- Indicators ---------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _hv(ticker: str, as_of: datetime.date, window: int = HV_WINDOW) -> float:
    """Annualised historical vol from daily closes."""
    try:
        start = as_of - datetime.timedelta(days=window * 3)
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=(as_of + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        close = df["Close"].dropna().astype(float)
        if len(close) < 5:
            return 0.40
        ret = np.log(close / close.shift(1)).dropna()
        return float(ret.tail(window).std() * math.sqrt(252))
    except Exception:
        return 0.40


# --- Main backtest ------------------------------------------------------------

def run_backtest(
    tickers: list[str]  = UNIVERSE,
    lookback_days: int  = LOOKBACK_DAYS,
    orb_minutes: int    = ORB_MINUTES,
) -> dict:

    orb_bars = max(1, orb_minutes // 5)

    print("\n" + "=" * 64)
    print("  ORB BACKTEST  -  Strategy 4: Opening Range Breakout")
    print(f"  Universe: {len(tickers)} stocks | Lookback: {lookback_days}d")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Risk/trade: {RISK_PER_TRADE_PCT:.0%}")
    print(f"  ORB: {orb_minutes} min | TP: +{TAKE_PROFIT_PCT:.0%} | SL: {STOP_LOSS_PCT:.0%}")
    print(f"  Exit: RSI>={RSI_OVERBOUGHT} | Vol fade <{VOLUME_FADE_RATIO:.0%} | EOD")
    print("=" * 64 + "\n")

    equity = STARTING_CAPITAL
    trades: list[dict] = []
    equity_curve: list[dict] = []

    for ticker in tickers:
        print(f"[{ticker}] downloading 5m...", end=" ", flush=True)

        try:
            raw = yf.download(
                ticker,
                period=f"{lookback_days}d",
                interval="5m",
                progress=False,
                auto_adjust=True,
            )
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]
            raw = raw.dropna()
        except Exception as e:
            print(f"error: {e}")
            continue

        if raw.empty or len(raw) < orb_bars + RSI_PERIOD + 5:
            print("insufficient data")
            continue

        raw.index = pd.to_datetime(raw.index)
        raw["_date"] = raw.index.date
        days = sorted(raw["_date"].unique())

        # Compute HV once per ticker from the end of the download window
        ticker_hv = _hv(ticker, days[-1])

        # Build daily open lookup for gap filter (use 1d data or first bar of each day)
        daily_open  = {}   # date -> first bar open
        daily_close = {}   # date -> last bar close (for next day gap calc)
        for d in days:
            d_bars = raw[raw["_date"] == d]
            if not d_bars.empty:
                daily_open[d]  = float(d_bars.iloc[0]["Open"])
                daily_close[d] = float(d_bars.iloc[-1]["Close"])

        ticker_trades = 0

        for idx_d, day in enumerate(days):
            day_bars = raw[raw["_date"] == day].copy()
            if len(day_bars) < orb_bars + RSI_PERIOD + 2:
                continue

            # Gap-up filter: today's open > yesterday's close
            if idx_d > 0:
                prev_day   = days[idx_d - 1]
                prev_close = daily_close.get(prev_day, 0.0)
                today_open = daily_open.get(day, 0.0)
                if prev_close > 0 and today_open <= prev_close:
                    continue   # No gap up — skip day

            # Compute RSI across the whole day
            day_bars = day_bars.copy()
            day_bars["rsi"] = _rsi(day_bars["Close"].astype(float), RSI_PERIOD)

            # Define opening range
            orb_slice  = day_bars.iloc[:orb_bars]
            orb_high   = float(orb_slice["High"].max())
            orb_low    = float(orb_slice["Low"].min())
            orb_avg_vol = float(orb_slice["Volume"].mean())

            if orb_high <= 0 or orb_avg_vol <= 0:
                continue

            # ORB width filter: ignore narrow, low-conviction ranges
            orb_mid = (orb_high + orb_low) / 2.0
            if orb_mid > 0 and (orb_high - orb_low) / orb_mid < MIN_ORB_WIDTH_PCT:
                continue

            # Scan for breakout candle within entry window (after ORB, first 2 hrs)
            entry_found = False
            entry_i     = None

            scan_end = min(len(day_bars), orb_bars + MAX_ENTRY_BARS)
            for i in range(orb_bars, scan_end):
                bar        = day_bars.iloc[i]
                bar_close  = float(bar["Close"])
                bar_volume = float(bar["Volume"])

                if bar_close > orb_high and bar_volume >= orb_avg_vol * VOLUME_SPIKE_MULT:
                    entry_found = True
                    entry_i     = i
                    break

            if not entry_found:
                continue

            # --- Entry --------------------------------------------------------
            entry_bar    = day_bars.iloc[entry_i]
            entry_price  = float(entry_bar["Close"])
            entry_vol    = float(entry_bar["Volume"])
            entry_ts     = day_bars.index[entry_i]
            strike       = round(entry_price)
            T_yr         = DTE_DAYS / 365.0

            opt_entry    = _bs_call(entry_price, strike, T_yr, ticker_hv)
            delta_e      = _bs_delta(entry_price, strike, T_yr, ticker_hv)
            gamma_e      = _bs_gamma(entry_price, strike, T_yr, ticker_hv)

            budget    = equity * RISK_PER_TRADE_PCT
            contracts = max(1, int(budget / (opt_entry * 100)))

            # --- Simulate exit ------------------------------------------------
            exit_price  = entry_price
            exit_opt    = opt_entry
            exit_reason = "eod"
            exit_ts     = day_bars.index[-1]

            for j in range(entry_i + 1, len(day_bars)):
                jbar    = day_bars.iloc[j]
                jclose  = float(jbar["Close"])
                jvol    = float(jbar["Volume"])
                jrsi    = float(jbar["rsi"]) if not pd.isna(jbar["rsi"]) else 50.0

                # Delta-gamma approximation for option price at bar j
                dS      = jclose - entry_price
                opt_j   = max(0.01, opt_entry + delta_e * dS + 0.5 * gamma_e * dS ** 2)
                pnl_pct = (opt_j - opt_entry) / opt_entry

                if pnl_pct >= TAKE_PROFIT_PCT:
                    exit_price  = jclose
                    exit_opt    = opt_entry * (1.0 + TAKE_PROFIT_PCT)
                    exit_reason = "take_profit"
                    exit_ts     = day_bars.index[j]
                    break

                if pnl_pct <= STOP_LOSS_PCT:
                    exit_price  = jclose
                    exit_opt    = opt_entry * (1.0 + STOP_LOSS_PCT)
                    exit_reason = "stop_loss"
                    exit_ts     = day_bars.index[j]
                    break

                if jrsi >= RSI_OVERBOUGHT:
                    exit_price  = jclose
                    exit_opt    = opt_j
                    exit_reason = "rsi_exit"
                    exit_ts     = day_bars.index[j]
                    break

                if jvol < entry_vol * VOLUME_FADE_RATIO and pnl_pct >= MIN_PROFIT_FOR_VOL_FADE:
                    exit_price  = jclose
                    exit_opt    = opt_j
                    exit_reason = "vol_fade"
                    exit_ts     = day_bars.index[j]
                    break

            else:
                last    = day_bars.iloc[-1]
                dS      = float(last["Close"]) - entry_price
                exit_price = float(last["Close"])
                exit_opt   = max(0.01, opt_entry + delta_e * dS + 0.5 * gamma_e * dS ** 2)

            final_pnl_pct = (exit_opt - opt_entry) / opt_entry
            pnl_dollar    = round(contracts * opt_entry * 100 * final_pnl_pct, 2)
            equity       += pnl_dollar
            ticker_trades += 1

            stock_move = (exit_price - entry_price) / entry_price * 100

            print(
                f"\n  {'WIN' if pnl_dollar > 0 else 'LOSS'}  {ticker} {day}"
                f" | ORB hi: {orb_high:.2f}"
                f" | Entry: {entry_price:.2f} -> Exit: {exit_price:.2f} ({stock_move:+.1f}%)"
                f" | Opt: ${opt_entry:.2f}->${exit_opt:.2f}"
                f" | P&L: ${pnl_dollar:+.0f} ({exit_reason})"
            )

            trades.append({
                "ticker":       ticker,
                "date":         str(day),
                "entry_ts":     str(entry_ts),
                "exit_ts":      str(exit_ts),
                "orb_high":     round(orb_high, 2),
                "orb_low":      round(orb_low, 2),
                "entry_price":  round(entry_price, 2),
                "exit_price":   round(exit_price, 2),
                "stock_move_pct": round(stock_move, 2),
                "strike":       strike,
                "hv_pct":       round(ticker_hv * 100, 1),
                "opt_entry":    round(opt_entry, 2),
                "opt_exit":     round(exit_opt, 2),
                "contracts":    contracts,
                "pnl_pct":      round(final_pnl_pct * 100, 2),
                "pnl_dollar":   pnl_dollar,
                "exit_reason":  exit_reason,
                "equity_after": round(equity, 2),
                "win":          pnl_dollar > 0,
            })

            equity_curve.append({"date": str(day), "equity": round(equity, 2)})

        if ticker_trades == 0:
            print("no signals")
        else:
            print(f"\n  [{ticker}] {ticker_trades} trades")

    # --- Summary --------------------------------------------------------------

    if not trades:
        print("\n[ORB] No trades generated.")
        return {}

    df     = pd.DataFrame(trades)
    total  = len(df)
    wins   = int(df["win"].sum())
    losses = total - wins
    wr     = round(wins / total * 100, 1)

    avg_pnl  = round(float(df["pnl_dollar"].mean()), 2)
    avg_win  = round(float(df[df["win"]]["pnl_dollar"].mean()), 2)  if wins   else 0.0
    avg_loss = round(float(df[~df["win"]]["pnl_dollar"].mean()), 2) if losses else 0.0

    total_pnl = round(float(df["pnl_dollar"].sum()), 2)
    total_ret = round((equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 1)

    vals   = [STARTING_CAPITAL] + list(df["equity_after"])
    peak   = STARTING_CAPITAL
    max_dd = 0.0
    for v in vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd
    max_dd = round(max_dd, 1)

    reasons = df["exit_reason"].value_counts().to_dict()
    avg_hold_bars = round(float(
        df.apply(lambda r: (
            (pd.Timestamp(r["exit_ts"]) - pd.Timestamp(r["entry_ts"])).seconds // 300
        ), axis=1).mean()
    ), 1) if total > 0 else 0

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
        "exit_reasons":     reasons,
        "avg_hold_bars":    avg_hold_bars,
        "period_days":      lookback_days,
    }

    print("\n" + "=" * 64)
    print("  ORB BACKTEST RESULTS")
    print("=" * 64)
    print(f"  Period:         Last {lookback_days} days")
    print(f"  Universe:       {len(tickers)} stocks")
    print(f"  Total trades:   {total}")
    print(f"  Win rate:       {wr}%  ({wins}W / {losses}L)")
    print(f"  Avg P&L:        ${avg_pnl:+,.2f} per trade")
    print(f"  Avg win:        ${avg_win:+,.2f}")
    print(f"  Avg loss:       ${avg_loss:+,.2f}")
    print(f"  Total P&L:      ${total_pnl:+,.2f}")
    print(f"  Total return:   {total_ret:+.1f}%")
    print(f"  Max drawdown:   {max_dd:.1f}%")
    print(f"  Final equity:   ${equity:,.2f}")
    print(f"  Avg hold time:  {avg_hold_bars} bars (~{avg_hold_bars*5:.0f} min)")
    print(f"  Exit reasons:   {reasons}")
    print("=" * 64 + "\n")

    df.to_csv("backtest_orb.csv", index=False)
    print("[ORB] Trade log saved -> backtest_orb.csv")

    with open("backtest_orb.json", "w") as f:
        json.dump({"summary": summary, "equity_curve": equity_curve, "trades": trades}, f, indent=2)
    print("[ORB] Equity curve saved -> backtest_orb.json\n")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ORB Strategy 4 Backtest")
    parser.add_argument("--tickers",     nargs="+", default=UNIVERSE,
                        help="Tickers to test (default: 30-stock universe)")
    parser.add_argument("--days",        type=int,  default=LOOKBACK_DAYS,
                        help=f"Lookback in calendar days (max ~60 for 5m data)")
    parser.add_argument("--orb-minutes", type=int,  default=ORB_MINUTES,
                        choices=[5, 10, 15],
                        help="Opening range window in minutes (default 15)")
    args = parser.parse_args()

    run_backtest(
        tickers     = args.tickers,
        lookback_days = args.days,
        orb_minutes = args.orb_minutes,
    )
