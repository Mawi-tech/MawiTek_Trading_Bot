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
    from mawitek.strategies.iv_rank_bot import run_iv_rank_scan, execute_iv_rank_trade
"""

import math
import datetime
import time
import uuid

import numpy as np
import pandas as pd
import yfinance as yf

from mawitek.data.universe import load_universe
from mawitek.data.market_filter import filter_universe
from mawitek.data.tradier_client import (
    get_options_expirations, get_options_chain, get_quote,
    get_open_positions, get_option_mid, MOCK_MODE,
)
from mawitek.core.order_manager import place_and_confirm, recover_pending_orders
from mawitek.core.risk_manager import pre_trade_check, record_trade, reconcile_from_broker
from mawitek.core.trade_journal import record_closed_trade
from mawitek.infra.state_io import file_lock, atomic_write_json, read_json
from mawitek.infra.utils import now_est, today_est
from mawitek.infra.heartbeat import beat


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


# --- Exit Config ---------------------------------------------------------------
# Credit spreads and long straddles need DIFFERENT exit rules than long calls.
# A credit spread is profitable when you can BUY IT BACK for less than the credit
# you received; a long straddle profits on a big move in either direction.

IVR_POSITIONS_FILE  = "iv_rank_positions.json"

# Credit spread (bull put): manage by % of the credit received.
SPREAD_TP_PCT       = 0.50   # Close after capturing 50% of the credit (buy back at 50% of entry credit)
SPREAD_SL_MULT      = 2.0    # Stop when the spread costs 2x the credit to close (≈ -1x credit loss)

# Long straddle: manage by % move on the debit paid.
STRADDLE_TP_PCT     = 0.50   # Close at +50% on the debit
STRADDLE_SL_PCT     = 0.50   # Close at -50% on the debit

# Both: never carry into the gamma/assignment danger zone near expiry.
IVR_MIN_DTE_EXIT    = 7      # Force-close any IV-rank position with <= this many DTE

# Sell-premium structure preference. An iron condor (sell an OTM put spread AND
# an OTM call spread) is delta-neutral and collects premium from both sides, so
# it's preferred over a one-sided bull-put spread when both wings are available.
# Falls back to the bull-put spread automatically if the call wing can't be built.
PREFER_IRON_CONDOR  = True

# Entry-leg limit price buffer. Legs used to be priced EXACTLY at mid, and in
# practice those orders sat unfilled for the whole 30s window and got cancelled
# (Jun 10 2026: five entry attempts in one session, zero fills). Crossing a few
# percent toward the far side of the spread trades a small amount of edge for
# actually getting filled: buys at mid*(1+x), sells at mid*(1-x).
LEG_FILL_BUFFER     = 0.04


# --- Position Book -------------------------------------------------------------
# IV-rank positions are multi-leg, so they can't live in open_positions.json
# (which is keyed by a single option symbol and exit-managed as a long call).
# We keep our own list-of-positions book, like the HFT strategy does.

def _load_iv_positions() -> list[dict]:
    data = read_json(IVR_POSITIONS_FILE, [])
    return data if isinstance(data, list) else []


def _save_iv_positions(positions: list[dict]) -> None:
    atomic_write_json(IVR_POSITIONS_FILE, positions)


def _add_iv_position(pos: dict) -> None:
    with file_lock(IVR_POSITIONS_FILE):
        positions = _load_iv_positions()
        positions.append(pos)
        _save_iv_positions(positions)
    print(f"[IVRank] Recorded position {pos['id']} | {pos['ticker']} {pos['strategy']} x{pos['quantity']}")


def _remove_iv_position(pos_id: str) -> None:
    with file_lock(IVR_POSITIONS_FILE):
        positions = [p for p in _load_iv_positions() if p.get("id") != pos_id]
        _save_iv_positions(positions)


# Live mid price for one option leg — shared with the other strategies.
_leg_mid = get_option_mid


def _dte_for(expiration: str) -> int:
    try:
        exp = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp - now_est().date()).days
    except Exception:
        return 999


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

    today = today_est()    # ET — DTE math should rollover with the market
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

    today = today_est()    # ET — match get_atm_iv's DTE window anchor
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
        if PREFER_IRON_CONDOR:
            condor = _select_iron_condor(chain, stock_price, target_exp, dte)
            if condor:
                return condor
            # else fall through to the one-sided bull-put spread
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


def _select_iron_condor(
    chain: list[dict],
    stock_price: float,
    expiration: str,
    dte: int,
) -> dict | None:
    """
    Iron condor: sell an OTM put spread AND an OTM call spread on the same
    expiry. Delta-neutral, defined-risk, collects premium from both sides.

    Strikes (symmetric, mirroring the bull-put spread's 5%/10% placement):
        short put  ≈ 5%  below spot   long put  ≈ 10% below spot
        short call ≈ 5%  above spot   long call ≈ 10% above spot

    Max profit (price expires between the short strikes) = net credit.
    Max loss = widest wing − net credit (defined).
    """
    puts  = [c for c in chain if c.get("option_type") == "put"]
    calls = [c for c in chain if c.get("option_type") == "call"]
    if not puts or not calls:
        return None

    def _best(legs: list[dict], target: float) -> dict | None:
        candidates = [
            c for c in legs
            if int(c.get("open_interest", 0) or 0) >= MIN_OI_PER_LEG
            and float(c.get("bid", 0) or 0) > 0
            and float(c.get("ask", 0) or 0) > 0
            and _spread_quality(float(c.get("bid", 0)), float(c.get("ask", 0))) <= MAX_SPREAD_PCT
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(float(c.get("strike", 0)) - target))

    short_put  = _best(puts,  stock_price * 0.95)
    long_put   = _best(puts,  stock_price * 0.90)
    short_call = _best(calls, stock_price * 1.05)
    long_call  = _best(calls, stock_price * 1.10)
    if not all([short_put, long_put, short_call, long_call]):
        return None

    sp_k = float(short_put.get("strike", 0));  lp_k = float(long_put.get("strike", 0))
    sc_k = float(short_call.get("strike", 0)); lc_k = float(long_call.get("strike", 0))

    # Structure must be strictly ordered: long put < short put < short call < long call.
    # (Strict ordering also guarantees all four legs are distinct contracts.)
    if not (lp_k < sp_k < sc_k < lc_k):
        return None

    def _mid(leg: dict) -> float:
        return (float(leg.get("bid", 0)) + float(leg.get("ask", 0))) / 2

    net_credit = round((_mid(short_put) - _mid(long_put)) + (_mid(short_call) - _mid(long_call)), 2)
    if net_credit <= 0:
        return None

    put_width  = sp_k - lp_k
    call_width = lc_k - sc_k
    max_risk = round((max(put_width, call_width) - net_credit) * 100, 2)
    if max_risk <= 0:
        return None  # credit ≥ width → bad/stale quotes; skip

    print(
        f"[IVRank] Iron condor | "
        f"Put ${sp_k:.0f}/{lp_k:.0f} + Call ${sc_k:.0f}/{lc_k:.0f} | "
        f"Exp: {expiration} | DTE: {dte} | "
        f"Credit: ${net_credit:.2f} | Max risk: ${max_risk:.2f}"
    )

    return {
        "strategy":          "iron_condor",
        "expiration":        expiration,
        "dte":               dte,
        "short_put":         short_put,  "long_put":  long_put,
        "short_call":        short_call, "long_call": long_call,
        "short_put_strike":  sp_k, "long_put_strike":  lp_k,
        "short_call_strike": sc_k, "long_call_strike": lc_k,
        "net_credit":        net_credit,
        "max_risk":          max_risk,
        "short_put_symbol":  short_put.get("symbol"),
        "long_put_symbol":   long_put.get("symbol"),
        "short_call_symbol": short_call.get("symbol"),
        "long_call_symbol":  long_call.get("symbol"),
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

    risk = pre_trade_check(ticker, strategy="iv_rank")
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
    elif strategy == "iron_condor":
        return _execute_iron_condor(ticker, legs, budget)
    elif strategy == "long_straddle":
        return _execute_straddle(ticker, legs, budget)

    return False


def _execute_bull_put(ticker: str, legs: dict, budget: float) -> bool:
    """Place sell + buy legs for a bull-put credit spread."""
    max_risk_per_contract = legs["max_risk"]
    if max_risk_per_contract <= 0:
        return False

    quantity = max(1, int(budget // max_risk_per_contract))

    # Buy the protection leg FIRST. Ordering matters for a credit spread: if
    # we sold first and the buy failed, we'd be left holding a naked short.
    # Buying first means the worst case is an unused long put, not naked risk.
    buy_mid = round(
        (float(legs["buy_leg"].get("bid", 0)) +
         float(legs["buy_leg"].get("ask", 0))) / 2, 2
    )
    buy_fill = place_and_confirm(
        symbol=ticker,
        option_symbol=legs["buy_symbol"],
        side="buy_to_open",
        quantity=quantity,
        order_type="limit",
        price=round(buy_mid * (1 + LEG_FILL_BUFFER), 2),   # cross toward ask to fill
        strategy="iv_rank",
        fallback_price=buy_mid,
        timeout=30.0,
    )
    if not buy_fill.ok or buy_fill.filled_qty <= 0:
        print(f"[IVRank] Protection (buy) leg did not fill: {buy_fill.reason} — aborting, no naked risk.")
        return False

    # Match the sell quantity to whatever the buy leg actually filled, so the
    # spread is always balanced (no naked shorts from a partial buy fill).
    spread_qty = int(buy_fill.filled_qty)
    sell_mid = round(
        (float(legs["sell_leg"].get("bid", 0)) +
         float(legs["sell_leg"].get("ask", 0))) / 2, 2
    )
    sell_fill = place_and_confirm(
        symbol=ticker,
        option_symbol=legs["sell_symbol"],
        side="sell_to_open",
        quantity=spread_qty,
        order_type="limit",
        price=round(sell_mid * (1 - LEG_FILL_BUFFER), 2),   # cross toward bid to fill
        strategy="iv_rank",
        fallback_price=sell_mid,
        timeout=30.0,
    )
    if not sell_fill.ok or sell_fill.filled_qty <= 0:
        # The credit leg didn't fill — we hold a long put we didn't want.
        # Flag loudly; reconciliation will surface it on the dashboard.
        print(f"[IVRank] WARNING: sell leg did not fill ({sell_fill.reason}). "
              f"Holding {spread_qty} long ${legs['buy_strike']}P — review and close if undesired.")
        return False

    record_trade(ticker)

    # Record the position so the monitor can manage its exit. Entry credit is
    # the actual net credit we received (short fill − long fill), not the
    # pre-trade mid estimate.
    entry_credit = round(float(sell_fill.avg_fill_price) - float(buy_fill.avg_fill_price), 2)
    _add_iv_position({
        "id":           uuid.uuid4().hex[:12],
        "ticker":       ticker,
        "strategy":     "bull_put_spread",
        "expiration":   legs["expiration"],
        "quantity":     int(sell_fill.filled_qty),
        "entry_time":   now_est().isoformat(),
        "entry_credit": entry_credit if entry_credit > 0 else legs["net_credit"],
        "max_risk":     legs["max_risk"],
        "setup_score":  legs.get("setup_score"),
        "legs": [
            {"symbol": legs["sell_symbol"], "side": "short", "strike": legs["sell_strike"],
             "type": "put", "entry_price": float(sell_fill.avg_fill_price)},
            {"symbol": legs["buy_symbol"], "side": "long", "strike": legs["buy_strike"],
             "type": "put", "entry_price": float(buy_fill.avg_fill_price)},
        ],
    })

    print(
        f"[IVRank] [OK] Bull-put spread filled | {ticker} | "
        f"${legs['sell_strike']}P / ${legs['buy_strike']}P | "
        f"x{sell_fill.filled_qty} | Credit: ${entry_credit:.2f}/contract"
    )
    return True


def _execute_iron_condor(ticker: str, legs: dict, budget: float) -> bool:
    """
    Place a 4-leg iron condor. Leg order matters for risk: BUY both protective
    wings FIRST (long put + long call), THEN sell the two short legs. That way,
    if a short leg fails to fill, the worst case is owning long options
    (defined risk) — never a naked short.
    """
    max_risk = legs["max_risk"]
    if max_risk <= 0:
        return False
    quantity = max(1, int(budget // max_risk))

    def _leg_mid_est(leg: dict) -> float:
        return round((float(leg.get("bid", 0)) + float(leg.get("ask", 0))) / 2, 2)

    def _buy_px(mid: float) -> float:
        return round(mid * (1 + LEG_FILL_BUFFER), 2)   # cross toward ask to fill

    def _sell_px(mid: float) -> float:
        return round(mid * (1 - LEG_FILL_BUFFER), 2)   # cross toward bid to fill

    # ── 1. Buy the long put wing (protection) ────────────────────────────────
    lp_mid = _leg_mid_est(legs["long_put"])
    lp = place_and_confirm(
        symbol=ticker, option_symbol=legs["long_put_symbol"], side="buy_to_open",
        quantity=quantity, order_type="limit", price=_buy_px(lp_mid),
        strategy="iv_rank", fallback_price=lp_mid, timeout=30.0,
    )
    if not lp.ok or lp.filled_qty <= 0:
        print(f"[IVRank] Condor long-put leg did not fill: {lp.reason} — aborting, no risk taken.")
        return False
    qty = int(lp.filled_qty)   # everything else matches the first wing's fill

    # ── 2. Buy the long call wing (protection) ───────────────────────────────
    lc_mid = _leg_mid_est(legs["long_call"])
    lc = place_and_confirm(
        symbol=ticker, option_symbol=legs["long_call_symbol"], side="buy_to_open",
        quantity=qty, order_type="limit", price=_buy_px(lc_mid),
        strategy="iv_rank", fallback_price=lc_mid, timeout=30.0,
    )
    if not lc.ok or lc.filled_qty <= 0:
        print(f"[IVRank] WARNING: condor long-call leg failed ({lc.reason}). "
              f"Holding {qty} long ${legs['long_put_strike']:.0f}P — review/close if undesired.")
        return False
    qty = min(qty, int(lc.filled_qty))

    # ── 3. Sell the short put (credit) ───────────────────────────────────────
    sp_mid = _leg_mid_est(legs["short_put"])
    sp = place_and_confirm(
        symbol=ticker, option_symbol=legs["short_put_symbol"], side="sell_to_open",
        quantity=qty, order_type="limit", price=_sell_px(sp_mid),
        strategy="iv_rank", fallback_price=sp_mid, timeout=30.0,
    )
    if not sp.ok or sp.filled_qty <= 0:
        print(f"[IVRank] WARNING: condor short-put leg failed ({sp.reason}). "
              f"Holding the two long wings (defined risk) on {ticker} — review/close.")
        return False

    # ── 4. Sell the short call (credit) ──────────────────────────────────────
    sc_mid = _leg_mid_est(legs["short_call"])
    sc = place_and_confirm(
        symbol=ticker, option_symbol=legs["short_call_symbol"], side="sell_to_open",
        quantity=qty, order_type="limit", price=_sell_px(sc_mid),
        strategy="iv_rank", fallback_price=sc_mid, timeout=30.0,
    )
    if not sc.ok or sc.filled_qty <= 0:
        print(f"[IVRank] WARNING: condor short-call leg failed ({sc.reason}). "
              f"Holding long wings + short put (a bull-put spread + long call) on {ticker} — review/close.")
        return False

    record_trade(ticker)

    # Actual net credit from the four fills: (shorts received) − (longs paid).
    entry_credit = round(
        (float(sp.avg_fill_price) + float(sc.avg_fill_price))
        - (float(lp.avg_fill_price) + float(lc.avg_fill_price)), 2
    )
    _add_iv_position({
        "id":           uuid.uuid4().hex[:12],
        "ticker":       ticker,
        "strategy":     "iron_condor",
        "expiration":   legs["expiration"],
        "quantity":     int(qty),
        "entry_time":   now_est().isoformat(),
        "entry_credit": entry_credit if entry_credit > 0 else legs["net_credit"],
        "max_risk":     legs["max_risk"],
        "setup_score":  legs.get("setup_score"),
        "legs": [
            {"symbol": legs["short_put_symbol"],  "side": "short", "strike": legs["short_put_strike"],
             "type": "put",  "entry_price": float(sp.avg_fill_price)},
            {"symbol": legs["long_put_symbol"],   "side": "long",  "strike": legs["long_put_strike"],
             "type": "put",  "entry_price": float(lp.avg_fill_price)},
            {"symbol": legs["short_call_symbol"], "side": "short", "strike": legs["short_call_strike"],
             "type": "call", "entry_price": float(sc.avg_fill_price)},
            {"symbol": legs["long_call_symbol"],  "side": "long",  "strike": legs["long_call_strike"],
             "type": "call", "entry_price": float(lc.avg_fill_price)},
        ],
    })

    print(
        f"[IVRank] [OK] Iron condor filled | {ticker} | "
        f"${legs['short_put_strike']:.0f}/{legs['long_put_strike']:.0f}P + "
        f"${legs['short_call_strike']:.0f}/{legs['long_call_strike']:.0f}C | "
        f"x{qty} | Credit: ${entry_credit:.2f}/contract"
    )
    return True


def _execute_straddle(ticker: str, legs: dict, budget: float) -> bool:
    """Place call + put for a long straddle."""
    total_per_contract = legs["total_debit"]
    if total_per_contract <= 0:
        return False

    quantity = max(1, int(budget // total_per_contract))

    fills: dict[str, float] = {}   # side_label -> avg fill price
    filled_legs = 0
    for side_label, symbol, mid in [
        ("call", legs["call_symbol"], legs["call_mid"]),
        ("put",  legs["put_symbol"],  legs["put_mid"]),
    ]:
        fill = place_and_confirm(
            symbol=ticker,
            option_symbol=symbol,
            side="buy_to_open",
            quantity=quantity,
            order_type="limit",
            price=round(mid * 1.05, 2),   # 5% buffer for fills
            strategy="iv_rank",
            fallback_price=mid,
            timeout=30.0,
        )
        if not fill.ok or fill.filled_qty <= 0:
            print(f"[IVRank] {side_label} leg did not fill: {fill.reason}")
            if filled_legs > 0:
                print(f"[IVRank] WARNING: straddle is one-legged ({filled_legs}/2 filled). "
                      f"Review {ticker} — you hold a directional position, not a straddle.")
            return False
        fills[side_label] = float(fill.avg_fill_price)
        filled_legs += 1

    record_trade(ticker)

    entry_debit = round(fills["call"] + fills["put"], 2)
    _add_iv_position({
        "id":          uuid.uuid4().hex[:12],
        "ticker":      ticker,
        "strategy":    "long_straddle",
        "expiration":  legs["expiration"],
        "quantity":    int(quantity),
        "entry_time":  now_est().isoformat(),
        "entry_debit": entry_debit if entry_debit > 0 else (legs["total_debit"] / 100),
        "setup_score": legs.get("setup_score"),
        "legs": [
            {"symbol": legs["call_symbol"], "side": "long", "strike": float(legs["call_leg"].get("strike", 0)),
             "type": "call", "entry_price": fills["call"]},
            {"symbol": legs["put_symbol"], "side": "long", "strike": float(legs["put_leg"].get("strike", 0)),
             "type": "put", "entry_price": fills["put"]},
        ],
    })

    print(
        f"[IVRank] [OK] Long straddle filled | {ticker} | "
        f"x{quantity} | Debit: ${entry_debit:.2f}/contract"
    )
    return True


# --- Position Monitoring & Exits -----------------------------------------------

def _update_iv_position(updated: dict) -> None:
    """Replace a position in the book by id (used to persist partial-close progress)."""
    with file_lock(IVR_POSITIONS_FILE):
        positions = _load_iv_positions()
        for i, p in enumerate(positions):
            if p.get("id") == updated.get("id"):
                positions[i] = updated
                break
        _save_iv_positions(positions)


def _spread_cost_to_close(pos: dict) -> float | None:
    """
    Current net debit to buy back a defined-risk credit position:
    Σ(short leg mids) − Σ(long leg mids).

    Works for a 2-leg vertical (1 short, 1 long) AND a 4-leg iron condor
    (2 short, 2 long). Returns None if any SHORT leg can't be priced (we must
    buy those back, so we can't act without a quote).
    """
    cost = 0.0
    for leg in pos["legs"]:
        m = _leg_mid(leg["symbol"], pos["ticker"], pos["expiration"])
        if leg["side"] == "short":
            if m <= 0:
                return None
            cost += m
        else:
            cost -= max(m, 0.0)
    return round(cost, 2)


def _straddle_value(pos: dict) -> float | None:
    """Current combined value of a long straddle (call mid + put mid)."""
    total = 0.0
    seen = 0
    for leg in pos["legs"]:
        m = _leg_mid(leg["symbol"], pos["ticker"], pos["expiration"])
        if m > 0:
            total += m
            seen += 1
    return round(total, 2) if seen else None


def _spread_exit_decision(pos: dict) -> tuple[bool, str]:
    credit = float(pos.get("entry_credit", 0) or 0)
    dte = _dte_for(pos["expiration"])
    if dte <= IVR_MIN_DTE_EXIT:
        return True, f"DTE {dte} ≤ {IVR_MIN_DTE_EXIT} — closing to avoid assignment/gamma"
    if credit <= 0:
        return False, ""
    cost = _spread_cost_to_close(pos)
    if cost is None:
        return False, ""
    if cost <= credit * (1 - SPREAD_TP_PCT):
        captured = (credit - cost) / credit * 100
        return True, f"Take profit — captured {captured:.0f}% of credit"
    if cost >= credit * SPREAD_SL_MULT:
        loss = (cost - credit) / credit * 100
        return True, f"Stop loss — spread at {cost:.2f} ≥ {SPREAD_SL_MULT}x credit ({loss:.0f}% of credit)"
    return False, ""


def _straddle_exit_decision(pos: dict) -> tuple[bool, str]:
    debit = float(pos.get("entry_debit", 0) or 0)
    dte = _dte_for(pos["expiration"])
    if dte <= IVR_MIN_DTE_EXIT:
        return True, f"DTE {dte} ≤ {IVR_MIN_DTE_EXIT} — closing before theta/expiry"
    if debit <= 0:
        return False, ""
    val = _straddle_value(pos)
    if val is None:
        return False, ""
    pct = (val - debit) / debit
    if pct >= STRADDLE_TP_PCT:
        return True, f"Take profit (+{pct*100:.0f}% on debit)"
    if pct <= -STRADDLE_SL_PCT:
        return True, f"Stop loss ({pct*100:.0f}% on debit)"
    return False, ""


def monitor_iv_rank_positions() -> None:
    """Check every open IV-rank position and close those that hit an exit."""
    positions = _load_iv_positions()
    if not positions:
        return
    print(f"[IVRank] Monitoring {len(positions)} open IV-rank position(s)...")
    for pos in list(positions):
        try:
            # Iron condors share the credit-based exit logic with bull-put
            # spreads (both are defined-risk credit positions managed by % of
            # the credit / DTE).
            if pos["strategy"] in ("bull_put_spread", "iron_condor"):
                exit_now, reason = _spread_exit_decision(pos)
            elif pos["strategy"] == "long_straddle":
                exit_now, reason = _straddle_exit_decision(pos)
            else:
                continue
            if exit_now:
                print(f"[IVRank] Exiting {pos['ticker']} {pos['strategy']} — {reason}")
                _close_iv_position(pos, reason)
        except Exception as e:
            print(f"[IVRank] monitor error on {pos.get('id')}: {e}")


def _close_iv_position(pos: dict, reason: str) -> bool:
    """
    Close every leg of an IV-rank position at market, journal the result, and
    remove it from the book. Short legs are closed FIRST (risk reduction).

    Per-leg progress is persisted so a leg that already closed isn't re-sent
    if a later leg's order fails and we retry next cycle.
    """
    ticker = pos["ticker"]
    qty    = int(pos["quantity"])
    closed = set(pos.get("closed_legs", []))
    exit_prices = dict(pos.get("exit_prices", {}))

    legs_sorted = sorted(pos["legs"], key=lambda l: 0 if l["side"] == "short" else 1)
    for leg in legs_sorted:
        if leg["symbol"] in closed:
            continue
        side = "buy_to_close" if leg["side"] == "short" else "sell_to_close"
        fill = place_and_confirm(
            symbol=ticker, option_symbol=leg["symbol"], side=side,
            quantity=qty, order_type="market", strategy="iv_rank",
            fallback_price=float(leg.get("entry_price", 0) or 0), timeout=20.0,
        )
        if fill.ok and fill.filled_qty > 0:
            closed.add(leg["symbol"])
            exit_prices[leg["symbol"]] = float(fill.avg_fill_price)
        else:
            # Persist what we've closed so we don't double-close on retry.
            pos["closed_legs"] = list(closed)
            pos["exit_prices"] = exit_prices
            _update_iv_position(pos)
            print(f"[IVRank] Could not close leg {leg['symbol']} ({fill.reason}) — retrying next cycle")
            return False

    # All legs closed → compute true P&L and journal it.
    pnl_dollar, pnl_pct, entry_ref, exit_ref, rep_symbol = _compute_iv_pnl(pos, exit_prices)

    try:
        record_closed_trade(
            option_symbol=rep_symbol,
            underlying=ticker,
            entry_price=entry_ref,
            exit_price=exit_ref,
            quantity=qty,
            expiration=pos["expiration"],
            entry_time=pos.get("entry_time"),
            exit_reason=reason,
            setup_score=pos.get("setup_score"),
            signals={"strategy_detail": pos["strategy"],
                     "legs": [l["symbol"] for l in pos["legs"]]},
            strategy="iv_rank",
            pnl_dollar=round(pnl_dollar, 2),
            pnl_pct=round(pnl_pct, 2),
        )
    except Exception as e:
        print(f"[IVRank] Failed to journal IV-rank close for {pos['id']}: {e}")

    try:
        from mawitek.infra.event_notifier import notify_position_closed
        notify_position_closed(
            ticker=ticker,
            contract=f"{pos['strategy']} {pos['expiration']}",
            pnl_dollar=round(pnl_dollar, 2),
            pnl_pct=round(pnl_pct, 1),
            reason=reason,
            strategy="iv_rank",
        )
    except Exception as e:
        print(f"[IVRank] notify failed: {e}")

    _remove_iv_position(pos["id"])
    print(f"[IVRank] Closed {ticker} {pos['strategy']} | P&L: ${pnl_dollar:+,.2f} ({pnl_pct:+.1f}%) | {reason}")
    return True


def _compute_iv_pnl(pos: dict, exit_prices: dict) -> tuple[float, float, float, float, str]:
    """
    Return (pnl_dollar, pnl_pct, entry_ref, exit_ref, representative_symbol)
    for a fully-closed IV-rank position. P&L accounts for the spread/straddle
    structure (a credit spread profits when bought back below the credit).
    """
    qty = int(pos["quantity"])
    if pos["strategy"] in ("bull_put_spread", "iron_condor"):
        # Credit structures (1 or 2 short legs): cost to close = Σ short exits −
        # Σ long exits. Profit = credit received − cost to close. Works for both
        # a vertical and a 4-leg condor.
        credit = float(pos.get("entry_credit", 0) or 0)
        cost_to_close = sum(exit_prices.get(l["symbol"], 0.0) for l in pos["legs"] if l["side"] == "short") \
            - sum(exit_prices.get(l["symbol"], 0.0) for l in pos["legs"] if l["side"] == "long")
        cost_to_close = round(cost_to_close, 4)
        pnl_dollar = round((credit - cost_to_close) * 100 * qty, 2)
        pnl_pct = round((credit - cost_to_close) / credit * 100, 2) if credit > 0 else 0.0
        rep = next(l["symbol"] for l in pos["legs"] if l["side"] == "short")
        return pnl_dollar, pnl_pct, credit, cost_to_close, rep
    else:  # long_straddle
        debit = float(pos.get("entry_debit", 0) or 0)
        exit_value = round(sum(exit_prices.get(l["symbol"], 0.0) for l in pos["legs"]), 4)
        pnl_dollar = round((exit_value - debit) * 100 * qty, 2)
        pnl_pct = round((exit_value - debit) / debit * 100, 2) if debit > 0 else 0.0
        return pnl_dollar, pnl_pct, debit, round(exit_value, 4), pos["legs"][0]["symbol"]


def reconcile_iv_positions() -> int:
    """
    Drop any IV-rank position whose legs are no longer at the broker (closed
    externally / expired). Journals them as closed_externally. Runs at startup.
    """
    positions = _load_iv_positions()
    if not positions:
        return 0
    if MOCK_MODE:
        return 0
    try:
        # strict=True → a failed broker read raises instead of returning [],
        # so a transient outage can't make us journal every open spread as
        # closed_externally and orphan it from exit management.
        broker_syms = {p.get("symbol") for p in get_open_positions(strict=True) if p.get("symbol")}
    except Exception as e:
        print(f"[IVRank] reconcile: could not query broker: {e}")
        return 0

    stale = []
    for pos in positions:
        leg_syms = {l["symbol"] for l in pos["legs"]}
        if not (leg_syms & broker_syms):   # none of its legs are still open
            stale.append(pos)

    for pos in stale:
        print(f"[IVRank] Position {pos['id']} ({pos['ticker']} {pos['strategy']}) not at broker — journaling closed_externally")
        try:
            record_closed_trade(
                option_symbol=pos["legs"][0]["symbol"],
                underlying=pos["ticker"],
                entry_price=float(pos.get("entry_credit", pos.get("entry_debit", 0)) or 0),
                exit_price=0.0,
                quantity=int(pos["quantity"]),
                expiration=pos["expiration"],
                entry_time=pos.get("entry_time"),
                exit_reason="closed_externally",
                setup_score=pos.get("setup_score"),
                signals={"strategy_detail": pos["strategy"]},
                strategy="iv_rank",
                pnl_dollar=0.0,   # unknown — external close, mark flat
                pnl_pct=0.0,
            )
        except Exception as e:
            print(f"[IVRank] reconcile journal failed for {pos['id']}: {e}")
        _remove_iv_position(pos["id"])

    return len(stale)


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

    # Resolve any in-flight orders from a prior crash before reconciling state.
    try:
        for r in recover_pending_orders():
            if r.ok and r.filled_qty > 0:
                print(f"[IVRank] Recovered fill from prior session: {r.tag} — {r.reason}")
    except Exception as e:
        print(f"[IVRank] Pending-order recovery failed (non-fatal): {e}")

    # Drop IV-rank positions closed at the broker while we were down.
    try:
        n = reconcile_iv_positions()
        if n:
            print(f"[IVRank] Reconciled {n} stale IV-rank position(s) against broker")
    except Exception as e:
        print(f"[IVRank] IV position reconciliation failed (non-fatal): {e}")

    # Reconcile P&L and halt flag from broker in case of a prior crash today.
    reconcile_from_broker()

    while True:
        try:
            now = now_est()    # tz-aware, US/Eastern
            if now.weekday() >= 5:
                print("[IVRank] Weekend - sleeping 1h")
                beat("iv_rank_bot", status="idle")
                time.sleep(3600)
                continue

            market_open  = now.replace(hour=9,  minute=35, second=0, microsecond=0)
            market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if not (market_open <= now <= market_close):
                print("[IVRank] Market closed - sleeping 60s")
                beat("iv_rank_bot", status="idle")
                time.sleep(60)
                continue

            beat("iv_rank_bot", status="scanning")

            # Manage exits on existing positions BEFORE opening new ones.
            monitor_iv_rank_positions()

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
