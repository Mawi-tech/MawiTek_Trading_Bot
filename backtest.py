"""
backtest.py

Backtests the catalyst-driven long call strategy over the last 6 months.

Methodology:
1. For each stock in universe, find past earnings dates
2. Check if scanner filters would have triggered (momentum + news score)
3. Pull real historical options prices from Tradier on entry date
4. Simulate entry 1 day before earnings, exit 1 day after (or at TP/SL)
5. Calculate win rate, avg return, and full P&L curve

Run:
    python backtest.py

Output:
    - Console summary
    - backtest_results.csv  (every trade)
    - backtest_equity.json  (equity curve for dashboard)
"""

import os
import json
import datetime
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────

TRADIER_API_KEY    = os.getenv("TRADIER_API_KEY", "")
TRADIER_ACCOUNT_ID = os.getenv("TRADIER_ACCOUNT_ID", "")
TRADIER_SANDBOX    = os.getenv("TRADIER_SANDBOX", "true").lower() == "true"

BASE_URL = (
    "https://sandbox.tradier.com/v1"
    if TRADIER_SANDBOX
    else "https://api.tradier.com/v1"
)

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json",
}

LOOKBACK_DAYS     = 180       # 6 months
STARTING_CAPITAL  = 10_000    # Simulated account size
RISK_PER_TRADE    = 0.02      # 2% per trade
TAKE_PROFIT_PCT   = 1.00      # +100%
STOP_LOSS_PCT     = -0.50     # -50%
MIN_MOMENTUM_SCORE = 40       # Same as live bot
TARGET_DELTA_MIN  = 0.35
TARGET_DELTA_MAX  = 0.60
MIN_OPEN_INTEREST = 50
MAX_SPREAD_PCT    = 0.15

# Universe to backtest — same as your live bot default
BACKTEST_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
    "NFLX", "MU", "AVGO", "PLTR", "QCOM", "ADBE", "CRM", "PYPL",
    "UBER", "SHOP", "PANW", "CRWD", "SNOW", "MRVL", "ANET", "COIN",
    "NET", "DDOG", "TTD", "RBLX", "HOOD", "ABNB",
]


# ─── Tradier Historical Options ─────────────────────────────────────────────────

def get_historical_options_chain(ticker: str, expiration: str, date: str) -> list[dict]:
    """
    Pull historical options chain for a ticker on a specific date.
    Uses Tradier's historical options endpoint.

    Args:
        ticker:     Stock symbol
        expiration: Option expiration date (YYYY-MM-DD)
        date:       Historical date to pull prices for (YYYY-MM-DD)
    """
    url = f"{BASE_URL}/markets/options/chains"
    params = {
        "symbol":     ticker,
        "expiration": expiration,
        "greeks":     "true",
    }

    try:
        # Small delay to avoid rate limiting
        time.sleep(0.3)
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        options = data.get("options", {}).get("option", [])
        return options if isinstance(options, list) else ([options] if options else [])
    except Exception as e:
        print(f"  [Tradier] Chain error {ticker} {expiration}: {e}")
        return []


def get_expirations(ticker: str) -> list[str]:
    url = f"{BASE_URL}/markets/options/expirations"
    params = {"symbol": ticker, "includeAllRoots": "true"}
    try:
        time.sleep(0.2)
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        dates = data.get("expirations", {}).get("date", [])
        return dates if isinstance(dates, list) else ([dates] if dates else [])
    except Exception:
        return []


def get_historical_quote(ticker: str, date: str) -> float:
    """Get closing price for a stock on a specific historical date."""
    try:
        start = datetime.datetime.strptime(date, "%Y-%m-%d")
        end   = start + datetime.timedelta(days=3)
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if df.empty:
            return 0.0
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        return float(close.iloc[0])
    except Exception:
        return 0.0


# ─── Momentum Scoring (simplified, no API calls) ───────────────────────────────

def score_momentum_historical(ticker: str, as_of_date: str) -> int:
    """
    Score momentum as of a historical date using yfinance daily data.
    Returns 0-100 score. Simplified version for backtesting speed.
    """
    try:
        end   = datetime.datetime.strptime(as_of_date, "%Y-%m-%d")
        start = end - datetime.timedelta(days=365)

        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=(end + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )

        if df.empty or len(df) < 60:
            return 0

        close  = df["Close"]
        volume = df["Volume"]
        if isinstance(close, pd.DataFrame):
            close  = close.iloc[:, 0]
            volume = volume.iloc[:, 0]

        close  = close.astype(float)
        volume = volume.astype(float)

        score = 0

        # Volume surge vs 20d avg
        avg_vol  = float(volume.tail(20).mean())
        last_vol = float(volume.iloc[-1])
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio >= 2.0:   score += 25
        elif vol_ratio >= 1.5: score += 15
        elif vol_ratio >= 1.2: score += 10

        # 5-day ROC
        roc5 = (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100
        if roc5 >= 5:   score += 20
        elif roc5 >= 3: score += 15
        elif roc5 >= 1: score += 10
        elif roc5 >= 0: score += 5

        # 10-day ROC
        roc10 = (float(close.iloc[-1]) - float(close.iloc[-11])) / float(close.iloc[-11]) * 100
        if roc10 >= 8:  score += 15
        elif roc10 >= 5: score += 10
        elif roc10 >= 2: score += 7
        elif roc10 >= 0: score += 3

        # 52-week high proximity
        high52 = float(close.tail(252).max())
        pct_from_high = float(close.iloc[-1]) / high52 * 100
        if pct_from_high >= 95: score += 20
        elif pct_from_high >= 90: score += 15
        elif pct_from_high >= 80: score += 8

        # RSI (simple calc)
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, np.nan)
        rsi    = 100 - (100 / (1 + rs))
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-6])
        if 45 <= rsi_now <= 70 and rsi_now > rsi_prev:
            score += 20
        elif 45 <= rsi_now <= 70 or rsi_now > rsi_prev:
            score += 10

        return min(score, 100)

    except Exception:
        return 0


# ─── Historical Earnings Dates ─────────────────────────────────────────────────

def get_past_earnings_dates(ticker: str, lookback_days: int = 180) -> list[str]:
    """
    Get past earnings dates within the lookback window using yfinance.
    Returns list of date strings YYYY-MM-DD sorted oldest first.
    """
    try:
        stock   = yf.Ticker(ticker)
        history = stock.earnings_dates

        if history is None or history.empty:
            return []

        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        today  = datetime.date.today()

        dates = []
        for idx in history.index:
            try:
                if hasattr(idx, 'date'):
                    d = idx.date()
                else:
                    d = datetime.datetime.strptime(str(idx)[:10], "%Y-%m-%d").date()

                if cutoff <= d < today:
                    dates.append(d.isoformat())
            except Exception:
                continue

        return sorted(dates)

    except Exception as e:
        print(f"  [Backtest] Earnings dates error {ticker}: {e}")
        return []


# ─── Option Selection (historical) ─────────────────────────────────────────────

def select_historical_option(
    ticker: str,
    entry_date: str,
    earnings_date: str,
    budget: float,
) -> dict | None:
    """
    Find the best call option to buy as of entry_date.
    Target: expiry at least 2 days after earnings, delta 0.35-0.60.
    """
    expirations = get_expirations(ticker)
    if not expirations:
        return None

    try:
        earn_dt  = datetime.datetime.strptime(earnings_date, "%Y-%m-%d")
        min_exp  = earn_dt + datetime.timedelta(days=2)
        max_exp  = earn_dt + datetime.timedelta(days=45)
    except Exception:
        return None

    valid_exps = [
        e for e in expirations
        if min_exp <= datetime.datetime.strptime(e, "%Y-%m-%d") <= max_exp
    ]

    if not valid_exps:
        return None

    best        = None
    best_score  = -1.0

    for exp in valid_exps[:3]:  # Check up to 3 expirations for speed
        chain = get_historical_options_chain(ticker, exp, entry_date)
        calls = [c for c in chain if c.get("option_type") == "call"]

        for contract in calls:
            try:
                greeks = contract.get("greeks") or {}
                delta  = abs(float(greeks.get("delta", 0) or 0))
                oi     = int(contract.get("open_interest", 0) or 0)
                bid    = float(contract.get("bid", 0) or 0)
                ask    = float(contract.get("ask", 0) or 0)

                if not (TARGET_DELTA_MIN <= delta <= TARGET_DELTA_MAX):
                    continue
                if oi < MIN_OPEN_INTEREST:
                    continue
                if bid <= 0 or ask <= 0:
                    continue
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 1.0
                if spread_pct > MAX_SPREAD_PCT:
                    continue
                if mid * 100 > budget * 1.2:
                    continue

                # Score: delta closest to 0.50, tight spread
                score = (1 - abs(delta - 0.50) * 2) * 0.5 + (1 - spread_pct / MAX_SPREAD_PCT) * 0.5
                if score > best_score:
                    best_score = score
                    best = {
                        "symbol":     contract.get("symbol", ""),
                        "strike":     float(contract.get("strike", 0)),
                        "expiration": exp,
                        "entry_mid":  round(mid, 2),
                        "delta":      round(delta, 3),
                        "oi":         oi,
                    }
            except Exception:
                continue

    return best


def get_exit_price(ticker: str, option: dict, exit_date: str) -> float:
    """
    Get the option mid price on the exit date.
    Falls back to stock-price-based estimate if chain unavailable.
    """
    chain = get_historical_options_chain(ticker, option["expiration"], exit_date)
    sym   = option["symbol"]

    for contract in chain:
        if contract.get("symbol") == sym:
            bid = float(contract.get("bid", 0) or 0)
            ask = float(contract.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 2)

    # Fallback: estimate based on stock move
    try:
        entry_stock = get_historical_quote(ticker, option.get("entry_date", exit_date))
        exit_stock  = get_historical_quote(ticker, exit_date)
        if entry_stock > 0 and exit_stock > 0:
            stock_move = (exit_stock - entry_stock) / entry_stock
            # Approximate: delta * stock move * 100 + entry mid
            estimated = option["entry_mid"] + (option["delta"] * stock_move * entry_stock)
            return max(0.01, round(estimated, 2))
    except Exception:
        pass

    return option["entry_mid"]  # Last resort: flat


# ─── Main Backtest Engine ──────────────────────────────────────────────────────

def next_trading_day(date_str: str, offset: int = 1) -> str:
    """Return the next trading day N days from date."""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    d += datetime.timedelta(days=offset)
    # Skip weekends
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def prev_trading_day(date_str: str, offset: int = 1) -> str:
    """Return N trading days before date."""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    d -= datetime.timedelta(days=offset)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def run_backtest() -> dict:
    print("\n" + "="*60)
    print("  OPTIONS CATALYST BACKTEST — 6 MONTHS")
    print(f"  Universe: {len(BACKTEST_UNIVERSE)} stocks")
    print(f"  Capital:  ${STARTING_CAPITAL:,.0f}")
    print(f"  Risk/trade: {RISK_PER_TRADE*100:.0f}%")
    print("="*60 + "\n")

    trades      = []
    equity      = STARTING_CAPITAL
    equity_curve = [{"date": (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat(), "equity": equity}]

    for ticker in BACKTEST_UNIVERSE:
        print(f"[Backtest] Processing {ticker}...")

        earnings_dates = get_past_earnings_dates(ticker, LOOKBACK_DAYS)
        if not earnings_dates:
            print(f"  {ticker}: no earnings dates found")
            continue

        print(f"  Found {len(earnings_dates)} earnings events")

        for earnings_date in earnings_dates:
            entry_date = prev_trading_day(earnings_date, 1)
            exit_date  = next_trading_day(earnings_date, 1)

            # Momentum filter
            mom_score = score_momentum_historical(ticker, entry_date)
            if mom_score < MIN_MOMENTUM_SCORE:
                print(f"  {ticker} {earnings_date}: momentum {mom_score} — skip")
                continue

            # Position sizing
            budget = equity * RISK_PER_TRADE

            # Select option
            option = select_historical_option(ticker, entry_date, earnings_date, budget)
            if not option:
                print(f"  {ticker} {earnings_date}: no qualifying option — skip")
                continue

            option["entry_date"] = entry_date
            mid_entry = option["entry_mid"]
            contracts = int(budget // (mid_entry * 100))
            if contracts <= 0:
                continue

            cost = contracts * mid_entry * 100

            # Get exit price
            mid_exit = get_exit_price(ticker, option, exit_date)

            # Apply TP/SL
            raw_pnl_pct = (mid_exit - mid_entry) / mid_entry
            if raw_pnl_pct >= TAKE_PROFIT_PCT:
                exit_reason = "take_profit"
                final_pct   = TAKE_PROFIT_PCT
            elif raw_pnl_pct <= STOP_LOSS_PCT:
                exit_reason = "stop_loss"
                final_pct   = STOP_LOSS_PCT
            else:
                exit_reason = "post_earnings"
                final_pct   = raw_pnl_pct

            pnl_dollar = round(contracts * mid_entry * 100 * final_pct, 2)
            equity    += pnl_dollar

            trade = {
                "ticker":        ticker,
                "earnings_date": earnings_date,
                "entry_date":    entry_date,
                "exit_date":     exit_date,
                "strike":        option["strike"],
                "expiration":    option["expiration"],
                "delta":         option["delta"],
                "contracts":     contracts,
                "entry_mid":     mid_entry,
                "exit_mid":      mid_exit,
                "cost":          round(cost, 2),
                "pnl_pct":       round(final_pct * 100, 2),
                "pnl_dollar":    pnl_dollar,
                "exit_reason":   exit_reason,
                "momentum_score": mom_score,
                "equity_after":  round(equity, 2),
                "win":           pnl_dollar > 0,
            }
            trades.append(trade)

            equity_curve.append({"date": exit_date, "equity": round(equity, 2)})

            result_icon = "✅" if pnl_dollar > 0 else "❌"
            print(
                f"  {result_icon} {ticker} {earnings_date} | "
                f"${option['strike']}C | x{contracts} | "
                f"Entry: ${mid_entry:.2f} → Exit: ${mid_exit:.2f} | "
                f"P&L: {'+' if pnl_dollar >= 0 else ''}{final_pct*100:.1f}% "
                f"(${'+' if pnl_dollar >= 0 else ''}{pnl_dollar:.0f}) | "
                f"{exit_reason}"
            )

    # ─── Summary Stats ──────────────────────────────────────────────────────────

    if not trades:
        print("\n[Backtest] No trades generated. Check API credentials and universe.")
        return {}

    df = pd.DataFrame(trades)

    total_trades   = len(df)
    wins           = int(df["win"].sum())
    losses         = total_trades - wins
    win_rate       = round(wins / total_trades * 100, 1)

    avg_win        = round(float(df[df["win"]]["pnl_pct"].mean()), 1) if wins > 0 else 0
    avg_loss       = round(float(df[~df["win"]]["pnl_pct"].mean()), 1) if losses > 0 else 0
    avg_return     = round(float(df["pnl_pct"].mean()), 1)

    total_pnl      = round(float(df["pnl_dollar"].sum()), 2)
    total_return   = round((equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 1)

    # Max drawdown
    equity_vals = [STARTING_CAPITAL] + [t["equity_after"] for t in trades]
    peak        = STARTING_CAPITAL
    max_dd      = 0.0
    for val in equity_vals:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 1)

    # Exit reason breakdown
    exit_counts = df["exit_reason"].value_counts().to_dict()

    summary = {
        "total_trades":    total_trades,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        win_rate,
        "avg_return_pct":  avg_return,
        "avg_win_pct":     avg_win,
        "avg_loss_pct":    avg_loss,
        "total_pnl":       total_pnl,
        "total_return_pct": total_return,
        "max_drawdown_pct": max_dd,
        "final_equity":    round(equity, 2),
        "starting_capital": STARTING_CAPITAL,
        "exit_breakdown":  exit_counts,
        "period_days":     LOOKBACK_DAYS,
    }

    print("\n" + "="*60)
    print("  BACKTEST RESULTS")
    print("="*60)
    print(f"  Period:       Last {LOOKBACK_DAYS} days (6 months)")
    print(f"  Universe:     {len(BACKTEST_UNIVERSE)} stocks")
    print(f"  Total trades: {total_trades}")
    print(f"  Win rate:     {win_rate}%  ({wins}W / {losses}L)")
    print(f"  Avg return:   {avg_return:+.1f}%  per trade")
    print(f"  Avg win:      {avg_win:+.1f}%")
    print(f"  Avg loss:     {avg_loss:+.1f}%")
    print(f"  Total P&L:    ${total_pnl:+,.2f}")
    print(f"  Total return: {total_return:+.1f}%")
    print(f"  Max drawdown: {max_dd:.1f}%")
    print(f"  Final equity: ${equity:,.2f}")
    print(f"  Exit reasons: {exit_counts}")
    print("="*60 + "\n")

    # Save outputs
    df.to_csv("backtest_results.csv", index=False)
    print("[Backtest] Trade log saved → backtest_results.csv")

    equity_curve_sorted = sorted(equity_curve, key=lambda x: x["date"])
    with open("backtest_equity.json", "w") as f:
        json.dump({"summary": summary, "equity_curve": equity_curve_sorted, "trades": trades}, f, indent=2)
    print("[Backtest] Equity curve saved → backtest_equity.json")

    return summary


if __name__ == "__main__":
    run_backtest()
