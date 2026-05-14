"""
iv_rank_bot.py  -  Strategy 2: IV Rank Scanner & Executor

IV Rank measures where current Implied Volatility sits relative to its
own 52-week range:

    IV Rank = (IV_current - IV_52w_low) / (IV_52w_high - IV_52w_low) * 100

Interpretation:
    IVR >= 75  ->  IV is elevated  ->  SELL premium (credit spreads / iron condors)
    IVR <= 25  ->  IV is depressed ->  BUY premium (long straddles / pre-event)
    25-74      ->  neutral         ->  skip unless another catalyst present

Because Tradier doesn't expose a historical IV series, we approximate
IV Rank using 30-day Historical Volatility (HV30) as a proxy for the
IV surface. For each ticker we:

    1. Download 1 year of daily prices via yfinance
    2. Compute a rolling 30-day HV from log-returns
    3. Pull the current ATM IV from Tradier (nearest 30-DTE expiry)
    4. Calculate IV Rank against the HV rolling distribution
    5. Score and rank candidates

Run as a standalone scanner:
    python iv_rank_bot.py

Or import and call:
    from iv_rank_bot import run_iv_rank_scan, execute_iv_rank_trade
"""

import math
import datetime
import time
import os

import numpy as np
import pandas as pd
import yfinance as yf

from universe import load_universe
from market_filter import filter_universe
from tradier_client import (
    get_options_expirations, get_options_chain, get_quote,
    place_option_order, MOCK_MODE,
)
from risk_manager import pre_trade_check, calculate_contracts, record_trade
from position_manager import record_entry


# --- Strategy Config -----------------------------------------------------------

IVR_SELL_THRESHOLD  = 75    # IVR >= this -> sell premium candidate
IVR_BUY_THRESHOLD   = 25    # IVR <= this -> buy premium candidate
TARGET_DTE_MIN      = 20    # Minimum DTE for credit spread legs
TARGET_DTE_MAX      = 45    # Maximum DTE for credit spread legs
MIN_ATM_IV          = 0.20  # Skip if ATM IV < 20% (too cheap to sell)
MAX_ATM_IV          = 2.00  # Skip if ATM IV > 200% (meme stock / untradeable)
MIN_OI_PER_LEG      = 100   # Minimum open interest per contract leg
MAX_SPREAD_PCT      = 0.15  # Max bid/ask spread as % of mid
UNIVERSE_LIMIT      = 100   # Symbols per scan cycle
MIN_SETUP_SCORE     = 50    # Minimum score to execute
SCAN_INTERVAL_SEC   = 300   # Re-scan every 5 minutes


# --- HV / IV Rank Calculation --------------------------------------------------

def _download_prices(ticker: str, period: str = "1y") -> pd.Series:
    """Return daily close prices as a float Series."""
    df = yf.download(
        tickers=ticker,
        interval="1d",
        period=period,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    close = df.get("Close", pd.Series(dtype=float)).dropna().astype(float)
    return close


def compute_hv30(close: pd.Series) -> pd.Series:
    """
    Rolling 30-day Historical Volatility (annualised) from log-returns.
    Returns a Series of the same length as close (NaN for first 30 rows).
    """
    log_ret = np.log(close / close.shift(1))
    hv = log_ret.rolling(30).std() * math.sqrt(252)
    return hv


def get_atm_iv(ticker: str) -> float | None:
    """
    Pull the ATM call IV from the nearest expiry within TARGET_DTE range.
    Returns annualised IV as a float (e.g. 0.35 = 35%), or None on failure.
    """
    exps = get_options_expirations(ticker)
    if not exps:
        return None

    stock_price = get_quote(ticker)
    if stock_price <= 0:
        return None

    today = datetime.date.today()
    target_exp = None
    for exp in exps:
        exp_date = datetime.date.fromisoformat(exp)
        dte = (exp_date - today).days
        if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
            target_exp = exp
            break

    if not target_exp:
        return None

    chain = get_options_chain(ticker, target_exp)
    if not chain:
        return None

    calls = [c for c in chain if c.get("option_type") == "call"]
    if not calls:
        return None

    # Find the strike closest to current price (ATM)
    atm = min(
        calls,
        key=lambda c: abs(float(c.get("strike", 0)) - stock_price)
    )
    greeks = atm.get("greeks") or {}
    iv = greeks.get("mid_iv") or greeks.get("smv_vol")
    if iv is None:
        return None

    try:
        return float(iv)
    except (TypeError, ValueError):
        return None


def compute_iv_rank(ticker: str) -> dict:
    """
    Compute IV Rank and related metrics for a single ticker.

    Returns:
        {
            "ticker":       str,
            "iv_rank":      float  (0-100),
            "atm_iv":       float  (annualised, e.g. 0.35),
            "hv30_current": float,
            "hv30_52w_low": float,
            "hv30_52w_high":float,
            "signal":       "sell_premium" | "buy_premium" | "neutral",
            "error":        str | None,
        }
    """
    empty = {
        "ticker": ticker, "iv_rank": None, "atm_iv": None,
        "hv30_current": None, "hv30_52w_low": None, "hv30_52w_high": None,
        "signal": "neutral", "error": None,
    }

    # 1. Download price history
    close = _download_prices(ticker)
    if len(close) < 60:
        return {**empty, "error": "insufficient price history"}

    # 2. Rolling HV30
    hv = compute_hv30(close).dropna()
    if hv.empty:
        return {**empty, "error": "could not compute HV30"}

    hv_52w = hv.tail(252)
    hv_low  = float(hv_52w.min())
    hv_high = float(hv_52w.max())
    hv_now  = float(hv.iloc[-1])

    # 3. ATM IV from Tradier (MOCK_MODE -> fall back to HV as IV proxy)
    atm_iv = get_atm_iv(ticker)
    if atm_iv is None:
        # Use current HV as proxy when broker data unavailable
        atm_iv = hv_now

    # Sanity bounds
    if atm_iv < MIN_ATM_IV or atm_iv > MAX_ATM_IV:
        return {**empty,
                "atm_iv": round(atm_iv, 4),
                "error": f"ATM IV={atm_iv:.1%} outside tradeable range"}

    # 4. IV Rank against HV distribution
    iv_range = hv_high - hv_low
    if iv_range < 0.01:
        iv_rank = 50.0   # Flat vol regime - call it neutral
    else:
        iv_rank = max(0.0, min(100.0, (atm_iv - hv_low) / iv_range * 100))

    # 5. Signal
    if iv_rank >= IVR_SELL_THRESHOLD:
        signal = "sell_premium"
    elif iv_rank <= IVR_BUY_THRESHOLD:
        signal = "buy_premium"
    else:
        signal = "neutral"

    print(
        f"[IVRank] {ticker} | IVR: {iv_rank:.1f} | ATM IV: {atm_iv:.1%} | "
        f"HV30: {hv_now:.1%} | 52W HV [{hv_low:.1%}-{hv_high:.1%}] | "
        f"Signal: {signal}"
    )

    return {
        "ticker":        ticker,
        "iv_rank":       round(iv_rank, 1),
        "atm_iv":        round(atm_iv, 4),
        "hv30_current":  round(hv_now, 4),
        "hv30_52w_low":  round(hv_low, 4),
        "hv30_52w_high": round(hv_high, 4),
        "signal":        signal,
        "error":         None,
    }


# --- Setup Scoring -------------------------------------------------------------

def score_iv_setup(iv_rank: float, atm_iv: float, signal: str) -> int:
    """
    Composite score 0-100 for an IV Rank setup.

    Sell-premium setups score higher at very high IVR.
    Buy-premium setups score higher at very low IVR.
    """
    score = 0

    if signal == "sell_premium":
        # IVR extremity (up to 50 pts)
        if iv_rank >= 95:
            score += 50
        elif iv_rank >= 90:
            score += 40
        elif iv_rank >= 85:
            score += 30
        elif iv_rank >= 75:
            score += 20

        # Absolute IV level (up to 30 pts - prefer fat premium)
        if atm_iv >= 0.80:
            score += 30
        elif atm_iv >= 0.60:
            score += 25
        elif atm_iv >= 0.40:
            score += 20
        elif atm_iv >= 0.30:
            score += 10

        # DTE available within window (up to 20 pts - static bonus)
        score += 20

    elif signal == "buy_premium":
        # IVR extremity (up to 50 pts)
        if iv_rank <= 5:
            score += 50
        elif iv_rank <= 10:
            score += 40
        elif iv_rank <= 15:
            score += 30
        elif iv_rank <= 25:
            score += 20

        # Absolute IV cheap level (up to 30 pts)
        if atm_iv <= 0.15:
            score += 30
        elif atm_iv <= 0.20:
            score += 25
        elif atm_iv <= 0.25:
            score += 20
        elif atm_iv <= 0.30:
            score += 10

        score += 20   # Base bonus for being in range

    return min(score, 100)


# --- Option Leg Selector -------------------------------------------------------

def _spread_quality(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else 1.0


def select_credit_spread_legs(
    ticker: str,
    stock_price: float,
    signal: str,
) -> dict | None:
    """
    For SELL_PREMIUM: select a bull-put credit spread below the market.
    For BUY_PREMIUM:  select a long straddle (ATM call + ATM put).

    Returns a dict describing the legs, or None if nothing qualifies.
    """
    exps = get_options_expirations(ticker)
    if not exps:
        return None

    today = datetime.date.today()
    target_exp = None
    for exp in exps:
        exp_date = datetime.date.fromisoformat(exp)
        dte = (exp_date - today).days
        if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
            target_exp = exp
            break

    if not target_exp:
        return None

    chain = get_options_chain(ticker, target_exp)
    if not chain:
        return None

    dte = (datetime.date.fromisoformat(target_exp) - today).days

    if signal == "sell_premium":
        return _select_bull_put_spread(chain, stock_price, target_exp, dte)
    elif signal == "buy_premium":
        return _select_long_straddle(chain, stock_price, target_exp, dte)

    return None


def _select_bull_put_spread(
    chain: list[dict],
    stock_price: float,
    expiration: str,
    dte: int,
) -> dict | None:
    """
    Bull-put credit spread: sell an OTM put, buy a lower put for protection.
    Sell strike ~ 5% below current price. Buy strike ~ 10% below.
    """
    puts = [c for c in chain if c.get("option_type") == "put"]
    if not puts:
        return None

    sell_target = stock_price * 0.95
    buy_target  = stock_price * 0.90

    def _best_put(target: float) -> dict | None:
        candidates = [
            p for p in puts
            if int(p.get("open_interest", 0) or 0) >= MIN_OI_PER_LEG
            and float(p.get("bid", 0) or 0) > 0
            and float(p.get("ask", 0) or 0) > 0
            and _spread_quality(
                float(p.get("bid", 0)), float(p.get("ask", 0))
            ) <= MAX_SPREAD_PCT
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: abs(float(p.get("strike", 0)) - target))

    sell_leg = _best_put(sell_target)
    buy_leg  = _best_put(buy_target)

    if not sell_leg or not buy_leg:
        return None

    sell_strike = float(sell_leg.get("strike", 0))
    buy_strike  = float(buy_leg.get("strike", 0))

    if sell_strike <= buy_strike:
        return None  # Spread is inverted

    sell_mid = (float(sell_leg.get("bid", 0)) + float(sell_leg.get("ask", 0))) / 2
    buy_mid  = (float(buy_leg.get("bid", 0))  + float(buy_leg.get("ask", 0)))  / 2
    net_credit = round(sell_mid - buy_mid, 2)

    if net_credit <= 0:
        return None

    width  = sell_strike - buy_strike
    max_risk = round((width - net_credit) * 100, 2)

    print(
        f"[IVRank] Bull-put spread | "
        f"Sell ${sell_strike}P / Buy ${buy_strike}P | "
        f"Exp: {expiration} | DTE: {dte} | "
        f"Credit: ${net_credit:.2f} | Max risk: ${max_risk:.2f}"
    )

    return {
        "strategy":      "bull_put_spread",
        "expiration":    expiration,
        "dte":           dte,
        "sell_leg":      sell_leg,
        "buy_leg":       buy_leg,
        "sell_strike":   sell_strike,
        "buy_strike":    buy_strike,
        "net_credit":    net_credit,
        "max_risk":      max_risk,
        "sell_symbol":   sell_leg.get("symbol"),
        "buy_symbol":    buy_leg.get("symbol"),
    }


def _select_long_straddle(
    chain: list[dict],
    stock_price: float,
    expiration: str,
    dte: int,
) -> dict | None:
    """
    Long straddle: buy ATM call + buy ATM put.
    Used when IV is low - pay cheap premium before expected vol expansion.
    """
    calls = [c for c in chain if c.get("option_type") == "call"]
    puts  = [c for c in chain if c.get("option_type") == "put"]

    def _best_atm(legs: list[dict]) -> dict | None:
        candidates = [
            c for c in legs
            if int(c.get("open_interest", 0) or 0) >= MIN_OI_PER_LEG
            and float(c.get("bid", 0) or 0) > 0
            and float(c.get("ask", 0) or 0) > 0
            and _spread_quality(
                float(c.get("bid", 0)), float(c.get("ask", 0))
            ) <= MAX_SPREAD_PCT
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(float(c.get("strike", 0)) - stock_price))

    call_leg = _best_atm(calls)
    put_leg  = _best_atm(puts)

    if not call_leg or not put_leg:
        return None

    call_mid  = (float(call_leg.get("bid", 0)) + float(call_leg.get("ask", 0))) / 2
    put_mid   = (float(put_leg.get("bid", 0))  + float(put_leg.get("ask", 0)))  / 2
    total_debit = round((call_mid + put_mid) * 100, 2)

    print(
        f"[IVRank] Long straddle | "
        f"ATM ${float(call_leg.get('strike', 0)):.0f} | "
        f"Exp: {expiration} | DTE: {dte} | "
        f"Total debit: ${total_debit:.2f}"
    )

    return {
        "strategy":      "long_straddle",
        "expiration":    expiration,
        "dte":           dte,
        "call_leg":      call_leg,
        "put_leg":       put_leg,
        "call_mid":      call_mid,
        "put_mid":       put_mid,
        "total_debit":   total_debit,
        "call_symbol":   call_leg.get("symbol"),
        "put_symbol":    put_leg.get("symbol"),
    }


# --- Trade Execution -----------------------------------------------------------

def execute_iv_rank_trade(setup: dict) -> bool:
    """
    Execute a single IV Rank trade (credit spread or straddle).
    Returns True if all orders placed successfully.
    """
    ticker  = setup["ticker"]
    signal  = setup["signal"]
    score   = setup.get("setup_score", 0)

    print(f"\n[IVRank] --- Executing {ticker} | Signal: {signal} | Score: {score} ---")

    risk = pre_trade_check(ticker)
    if not risk["approved"]:
        print(f"[IVRank] {ticker} blocked: {risk['reason']}")
        return False

    budget  = risk["budget"]
    equity  = risk["equity"]

    stock_price = get_quote(ticker)
    if stock_price <= 0:
        print(f"[IVRank] Cannot get quote for {ticker}")
        return False

    legs = select_credit_spread_legs(ticker, stock_price, signal)
    if not legs:
        print(f"[IVRank] No qualifying legs found for {ticker}")
        return False

    strategy = legs["strategy"]

    if strategy == "bull_put_spread":
        return _execute_bull_put(ticker, legs, budget)
    elif strategy == "long_straddle":
        return _execute_straddle(ticker, legs, budget)

    return False


def _execute_bull_put(ticker: str, legs: dict, budget: float) -> bool:
    """Place sell + buy legs for a bull-put credit spread."""
    max_risk_per_contract = legs["max_risk"]
    if max_risk_per_contract <= 0:
        return False

    quantity = max(1, int(budget // max_risk_per_contract))

    # Sell leg first
    sell_order = place_option_order(
        symbol=ticker,
        option_symbol=legs["sell_symbol"],
        side="sell_to_open",
        quantity=quantity,
        order_type="limit",
        price=round(
            (float(legs["sell_leg"].get("bid", 0)) +
             float(legs["sell_leg"].get("ask", 0))) / 2, 2
        ),
    )
    if sell_order.get("status") == "error":
        print(f"[IVRank] Sell leg failed: {sell_order.get('error')}")
        return False

    # Buy leg (protection)
    buy_order = place_option_order(
        symbol=ticker,
        option_symbol=legs["buy_symbol"],
        side="buy_to_open",
        quantity=quantity,
        order_type="limit",
        price=round(
            (float(legs["buy_leg"].get("bid", 0)) +
             float(legs["buy_leg"].get("ask", 0))) / 2, 2
        ),
    )
    if buy_order.get("status") == "error":
        print(f"[IVRank] Buy leg failed - spread is now naked! Close sell leg manually.")
        return False

    record_trade(ticker)
    print(
        f"[IVRank] [OK] Bull-put spread placed | {ticker} | "
        f"${legs['sell_strike']}P / ${legs['buy_strike']}P | "
        f"x{quantity} | Credit: ${legs['net_credit']:.2f}/contract"
    )
    return True


def _execute_straddle(ticker: str, legs: dict, budget: float) -> bool:
    """Place call + put for a long straddle."""
    total_per_contract = legs["total_debit"]
    if total_per_contract <= 0:
        return False

    quantity = max(1, int(budget // total_per_contract))

    for side_label, symbol, mid in [
        ("call", legs["call_symbol"], legs["call_mid"]),
        ("put",  legs["put_symbol"],  legs["put_mid"]),
    ]:
        order = place_option_order(
            symbol=ticker,
            option_symbol=symbol,
            side="buy_to_open",
            quantity=quantity,
            order_type="limit",
            price=round(mid * 1.05, 2),   # 5% buffer for fills
        )
        if order.get("status") == "error":
            print(f"[IVRank] {side_label} leg failed: {order.get('error')}")
            return False

    record_trade(ticker)
    print(
        f"[IVRank] [OK] Long straddle placed | {ticker} | "
        f"x{quantity} | Debit: ${legs['total_debit']:.2f}"
    )
    return True


# --- Main Scanner --------------------------------------------------------------

def run_iv_rank_scan(
    csv_path: str | None = "sp500.csv",
    universe_limit: int = UNIVERSE_LIMIT,
    min_score: int = MIN_SETUP_SCORE,
    output_csv: bool = True,
) -> list[dict]:
    """
    Full IV Rank scan pipeline.

    1. Load + liquidity-filter universe
    2. Compute IV Rank for each ticker
    3. Score and rank setups
    4. Return sorted list

    Returns list of setup dicts sorted by score descending.
    """
    print("\n" + "=" * 60)
    print("  IV RANK SCANNER  -  Strategy 2")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60 + "\n")

    symbols = load_universe(csv_path=csv_path, limit=universe_limit)
    print(f"[IVRank] Loaded {len(symbols)} symbols")

    liquid = filter_universe(
        symbols=symbols,
        min_price=10.0,
        min_avg_volume=1_000_000,
        min_avg_dollar_volume=20_000_000,
    )
    print(f"[IVRank] {len(liquid)} symbols passed liquidity filter\n")

    results = []

    for ticker in liquid:
        ivr = compute_iv_rank(ticker)

        if ivr.get("error") or ivr.get("signal") == "neutral":
            continue

        iv_rank = ivr["iv_rank"]
        atm_iv  = ivr["atm_iv"]
        signal  = ivr["signal"]

        score = score_iv_setup(iv_rank, atm_iv, signal)
        if score < min_score:
            continue

        result = {
            "ticker":        ticker,
            "setup_score":   score,
            "signal":        signal,
            "iv_rank":       iv_rank,
            "atm_iv":        round(atm_iv * 100, 1),    # display as %
            "hv30_current":  round(ivr["hv30_current"] * 100, 1),
            "hv30_52w_low":  round(ivr["hv30_52w_low"] * 100, 1),
            "hv30_52w_high": round(ivr["hv30_52w_high"] * 100, 1),
        }
        results.append(result)
        print(
            f"[IVRank] [OK] {ticker} | Score: {score}/100 | "
            f"IVR: {iv_rank:.1f} | Signal: {signal}"
        )

    results.sort(key=lambda x: x["setup_score"], reverse=True)

    print("\n" + "=" * 60)
    print(f"  TOP IV RANK SETUPS ({len(results)} found)")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        arrow = "[!] SELL PREMIUM" if r["signal"] == "sell_premium" else "[+] BUY PREMIUM"
        print(
            f"\n#{i} {r['ticker']} | Score: {r['setup_score']}/100 | {arrow}"
            f"\n    IVR: {r['iv_rank']:.1f} | ATM IV: {r['atm_iv']:.1f}% | "
            f"HV30: {r['hv30_current']:.1f}% "
            f"[52W: {r['hv30_52w_low']:.1f}%-{r['hv30_52w_high']:.1f}%]"
        )

    if output_csv and results:
        df = pd.DataFrame(results)
        fname = f"iv_rank_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"\n[IVRank] Results saved to {fname}")

    return results


# --- Bot Loop ------------------------------------------------------------------

def run():
    """
    Standalone IV Rank bot loop.
    Scans, scores, and executes IV Rank trades on a repeating timer.
    """
    print("\n" + "=" * 60)
    print("  IV RANK BOT  -  Strategy 2  -  STARTING")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if MOCK_MODE:
        print("  [!]  MOCK_MODE - no real orders will be placed")
    print("=" * 60)

    while True:
        try:
            now = datetime.datetime.now()
            if now.weekday() >= 5:
                print("[IVRank] Weekend - sleeping 1h")
                time.sleep(3600)
                continue

            market_open  = now.replace(hour=9,  minute=35, second=0)
            market_close = now.replace(hour=15, minute=30, second=0)
            if not (market_open <= now <= market_close):
                print("[IVRank] Market closed - sleeping 60s")
                time.sleep(60)
                continue

            setups = run_iv_rank_scan(output_csv=False)
            trades = 0
            for setup in setups[:2]:   # Max 2 IV trades per cycle
                if execute_iv_rank_trade(setup):
                    trades += 1

            print(f"\n[IVRank] Cycle done | Trades: {trades} | "
                  f"Sleeping {SCAN_INTERVAL_SEC}s\n")

        except KeyboardInterrupt:
            print("\n[IVRank] Stopped by user.")
            break
        except Exception as e:
            print(f"[IVRank] Error: {e} - retrying in 60s")
            time.sleep(60)
            continue

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="IV Rank Scanner - Strategy 2")
    parser.add_argument("--scan-only", action="store_true",
                        help="Run one scan cycle and print results, no execution")
    parser.add_argument("--limit",     type=int, default=UNIVERSE_LIMIT,
                        help=f"Universe size (default {UNIVERSE_LIMIT})")
    args = parser.parse_args()

    if args.scan_only:
        run_iv_rank_scan(universe_limit=args.limit)
    else:
        run()
