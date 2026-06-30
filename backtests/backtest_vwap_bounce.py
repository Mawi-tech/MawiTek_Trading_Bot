"""
backtest_vwap_bounce.py  -  Strategy 3 (rebuilt): VWAP Bounce

Simple explanation:
  VWAP is the stock's "fair price" for the day — the average price weighted by volume.
  When a strong stock dips down to that fair price and then bounces back above it with
  buyers rushing in, that's a high-probability long entry. You're buying at support,
  not chasing a move that already happened.

Methodology:
  1. Gap-up filter: stock opens > 1.5% above prev close (strong catalyst day)
  2. After the first 30 minutes, compute rolling VWAP
  3. Wait for price to dip below VWAP (pullback), then close back above it
     with increasing volume (the bounce)
  4. Enter a near-term ATM call (DTE=7, BS-priced)
  5. Exit on: take profit (+70%), stop loss (-25%), RSI overbought (>= 70),
     price falls back below VWAP (bounce failed), or end of day

One trade per stock per day. Only trade in the first 3 hours after the bounce window.

Run:
    python backtest_vwap_bounce.py
    python backtest_vwap_bounce.py --tickers NVDA TSLA AMD --days 60
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

STARTING_CAPITAL    = 50_000
RISK_PER_TRADE_PCT  = 0.02      # 2% per trade

LOOKBACK_DAYS       = 60        # yfinance 5m limit
WARMUP_BARS         = 6         # Wait 30 min (6 x 5m bars) before scanning
MAX_ENTRY_BARS      = 36        # Only look for bounces in first 3 hrs after warmup

GAP_UP_PCT          = 0.015     # Stock must open > 1.5% above prev close
MIN_BOUNCE_VOL_MULT = 1.3       # Bounce bar volume must be > 1.3x the dip bar volume

TAKE_PROFIT_PCT     = 0.70      # +70% on option
STOP_LOSS_PCT       = -0.25     # -25% on option (same as ORB)
RSI_PERIOD          = 14
RSI_OVERBOUGHT      = 70
DTE_DAYS            = 7
HV_WINDOW           = 20

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


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Rolling intraday VWAP reset each day (applied to a single day's bars)."""
    typical = (high + low + close) / 3.0
    cum_vol  = volume.cumsum()
    cum_tpv  = (typical * volume).cumsum()
    return cum_tpv / cum_vol.replace(0.0, np.nan)


def _hv(ticker: str, as_of: datetime.date, window: int = HV_WINDOW) -> float:
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
    tickers: list[str] = UNIVERSE,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict:

    print("\n" + "=" * 64)
    print("  VWAP BOUNCE BACKTEST  -  Strategy 3 (rebuilt)")
    print(f"  Universe: {len(tickers)} stocks | Lookback: {lookback_days}d")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Risk/trade: {RISK_PER_TRADE_PCT:.0%}")
    print(f"  Gap filter: >{GAP_UP_PCT:.1%} | TP: +{TAKE_PROFIT_PCT:.0%} | SL: {STOP_LOSS_PCT:.0%}")
    print(f"  Bounce vol: >{MIN_BOUNCE_VOL_MULT}x dip bar | RSI exit: >={RSI_OVERBOUGHT}")
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

        if raw.empty or len(raw) < WARMUP_BARS + RSI_PERIOD + 5:
            print("insufficient data")
            continue

        raw.index = pd.to_datetime(raw.index)
        raw["_date"] = raw.index.date
        days = sorted(raw["_date"].unique())

        ticker_hv = _hv(ticker, days[-1])

        daily_open  = {}
        daily_close = {}
        for d in days:
            d_bars = raw[raw["_date"] == d]
            if not d_bars.empty:
                daily_open[d]  = float(d_bars.iloc[0]["Open"])
                daily_close[d] = float(d_bars.iloc[-1]["Close"])

        ticker_trades = 0

        for idx_d, day in enumerate(days):
            day_bars = raw[raw["_date"] == day].copy()
            if len(day_bars) < WARMUP_BARS + RSI_PERIOD + 2:
                continue

            # Gap-up filter: today must open above yesterday's close by GAP_UP_PCT
            if idx_d == 0:
                continue
            prev_day   = days[idx_d - 1]
            prev_close = daily_close.get(prev_day, 0.0)
            today_open = daily_open.get(day, 0.0)
            if prev_close <= 0 or today_open <= prev_close * (1.0 + GAP_UP_PCT):
                continue

            # Compute VWAP and RSI for the full day
            day_bars["vwap"] = _vwap(
                day_bars["High"].astype(float),
                day_bars["Low"].astype(float),
                day_bars["Close"].astype(float),
                day_bars["Volume"].astype(float),
            )
            day_bars["rsi"] = _rsi(day_bars["Close"].astype(float), RSI_PERIOD)

            # Scan for bounce pattern after warmup window, within first 3 hours
            scan_start = WARMUP_BARS
            scan_end   = min(len(day_bars) - 1, WARMUP_BARS + MAX_ENTRY_BARS)

            entry_found = False

            for i in range(scan_start, scan_end):
                bar      = day_bars.iloc[i]
                prev_bar = day_bars.iloc[i - 1]

                bar_close  = float(bar["Close"])
                bar_vwap   = float(bar["vwap"]) if not pd.isna(bar["vwap"]) else 0.0
                bar_vol    = float(bar["Volume"])
                prev_close = float(prev_bar["Close"])
                prev_vwap  = float(prev_bar["vwap"]) if not pd.isna(prev_bar["vwap"]) else 0.0
                prev_vol   = float(prev_bar["Volume"])

                if bar_vwap <= 0 or prev_vwap <= 0:
                    continue

                # Bounce pattern:
                #   Previous bar: closed BELOW VWAP (the dip)
                #   Current bar:  closes BACK ABOVE VWAP (the bounce)
                #   Volume on bounce bar > volume on dip bar (buyers rushing in)
                dip_happened    = prev_close < prev_vwap
                bounce_happened = bar_close > bar_vwap
                vol_confirmed   = prev_vol > 0 and bar_vol >= prev_vol * MIN_BOUNCE_VOL_MULT

                if dip_happened and bounce_happened and vol_confirmed:
                    entry_found = True
                    entry_i     = i
                    break

            if not entry_found:
                continue

            # --- Entry --------------------------------------------------------
            entry_bar    = day_bars.iloc[entry_i]
            entry_price  = float(entry_bar["Close"])
            entry_vwap   = float(entry_bar["vwap"])
            entry_ts     = day_bars.index[entry_i]
            strike       = round(entry_price)
            T_yr         = DTE_DAYS / 365.0

            opt_entry = _bs_call(entry_price, strike, T_yr, ticker_hv)
            delta_e   = _bs_delta(entry_price, strike, T_yr, ticker_hv)
            gamma_e   = _bs_gamma(entry_price, strike, T_yr, ticker_hv)

            budget    = equity * RISK_PER_TRADE_PCT
            contracts = max(1, int(budget / (opt_entry * 100)))

            # --- Simulate exit ------------------------------------------------
            exit_price  = entry_price
            exit_opt    = opt_entry
            exit_reason = "eod"
            exit_ts     = day_bars.index[-1]

            for j in range(entry_i + 1, len(day_bars)):
                jbar   = day_bars.iloc[j]
                jclose = float(jbar["Close"])
                jvwap  = float(jbar["vwap"]) if not pd.isna(jbar["vwap"]) else entry_vwap
                jrsi   = float(jbar["rsi"]) if not pd.isna(jbar["rsi"]) else 50.0

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

                # Bounce failed: price falls back below VWAP
                if jclose < jvwap and pnl_pct < 0:
                    exit_price  = jclose
                    exit_opt    = opt_j
                    exit_reason = "vwap_break"
                    exit_ts     = day_bars.index[j]
                    break

            else:
                last       = day_bars.iloc[-1]
                dS         = float(last["Close"]) - entry_price
                exit_price = float(last["Close"])
                exit_opt   = max(0.01, opt_entry + delta_e * dS + 0.5 * gamma_e * dS ** 2)

            final_pnl_pct = (exit_opt - opt_entry) / opt_entry
            pnl_dollar    = round(contracts * opt_entry * 100 * final_pnl_pct, 2)
            equity       += pnl_dollar
            ticker_trades += 1

            stock_move = (exit_price - entry_price) / entry_price * 100

            print(
                f"\n  {'WIN' if pnl_dollar > 0 else 'LOSS'}  {ticker} {day}"
                f" | VWAP: {entry_vwap:.2f} | Entry: {entry_price:.2f} -> Exit: {exit_price:.2f} ({stock_move:+.1f}%)"
                f" | Opt: ${opt_entry:.2f}->${exit_opt:.2f}"
                f" | P&L: ${pnl_dollar:+.0f} ({exit_reason})"
            )

            trades.append({
                "ticker":         ticker,
                "date":           str(day),
                "entry_ts":       str(entry_ts),
                "exit_ts":        str(exit_ts),
                "entry_vwap":     round(entry_vwap, 2),
                "entry_price":    round(entry_price, 2),
                "exit_price":     round(exit_price, 2),
                "stock_move_pct": round(stock_move, 2),
                "strike":         strike,
                "hv_pct":         round(ticker_hv * 100, 1),
                "opt_entry":      round(opt_entry, 2),
                "opt_exit":       round(exit_opt, 2),
                "contracts":      contracts,
                "pnl_pct":        round(final_pnl_pct * 100, 2),
                "pnl_dollar":     pnl_dollar,
                "exit_reason":    exit_reason,
                "equity_after":   round(equity, 2),
                "win":            pnl_dollar > 0,
            })

            equity_curve.append({"date": str(day), "equity": round(equity, 2)})

        if ticker_trades == 0:
            print("no signals")
        else:
            print(f"\n  [{ticker}] {ticker_trades} trades")

    # --- Summary --------------------------------------------------------------

    if not trades:
        print("\n[VWAP] No trades generated.")
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
        "period_days":      lookback_days,
    }

    print("\n" + "=" * 64)
    print("  VWAP BOUNCE BACKTEST RESULTS")
    print("=" * 64)
    print(f"  Period:       Last {lookback_days} days")
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
    print(f"  Exit reasons: {reasons}")
    print("=" * 64 + "\n")

    df.to_csv("backtest_vwap_bounce.csv", index=False)
    print("[VWAP] Trade log saved -> backtest_vwap_bounce.csv")

    with open("backtest_vwap_bounce.json", "w") as f:
        json.dump({"summary": summary, "equity_curve": equity_curve, "trades": trades}, f, indent=2)
    print("[VWAP] Equity curve saved -> backtest_vwap_bounce.json\n")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VWAP Bounce Strategy 3 Backtest")
    parser.add_argument("--tickers", nargs="+", default=UNIVERSE)
    parser.add_argument("--days",    type=int,  default=LOOKBACK_DAYS)
    args = parser.parse_args()

    run_backtest(tickers=args.tickers, lookback_days=args.days)
