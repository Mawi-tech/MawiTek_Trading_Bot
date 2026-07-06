"""
vwap_fade_scanner.py  —  Strategy 6: Intraday VWAP Mean-Reversion Fade

The complement to the HFT momentum scalper (Strategy 3). Where HFT buys BREAKOUTS
(and therefore goes quiet on calm / range-bound days), this strategy FADES
over-extension: when a liquid name gets stretched far from its intraday VWAP and
then STALLS, it bets on reversion back toward VWAP.

    Stretched ABOVE VWAP + overbought + a stall/reversal bar  ->  buy a PUT
    Stretched BELOW VWAP + oversold  + a stall/reversal bar   ->  buy a CALL

It is deliberately bidirectional (a real fade needs both sides) and is therefore
shipped DARK behind ENABLE_VWAP_FADE until backtest_vwap_fade.py validates it on
two independent samples — the same discipline as ENABLE_BEAR_CALL / the rejected
HFT-bidirectional experiment (ARCHITECTURE §1).

Design notes:
  • STRETCH is measured as a volatility-normalized z-score — the session's own
    rolling stdev of (Close − VWAP) — not a fixed %, so the trigger is comparable
    across a $40 name and a $900 name and adapts to the day's realized range. An
    absolute %-of-price floor stops a dead-flat name from triggering on noise.
  • The KILLER RISK is a trend day: a fade gets run over when the move keeps going.
    is_trend_day() is the dedicated gate — it skips the fade whenever the day is
    trending in the direction OPPOSING the fade (Kaufman efficiency ratio, a
    persistent one-side-of-VWAP session, or a steep EMA20 slope).

Reuses the HFT scanner's indicators (compute_vwap/rsi/ema), intraday fetch, and
universe/liquidity plumbing rather than duplicating them.

Usage:
    from vwap_fade_scanner import run_vwap_fade_scan
    setups = run_vwap_fade_scan()          # [] while ENABLE_VWAP_FADE is False

    python vwap_fade_scanner.py
"""

import datetime
import argparse

import numpy as np
import pandas as pd

from universe import load_universe
from market_filter import filter_universe
from logger import get_logger

# Reuse the HFT scanner's indicators + intraday fetch + inverse-ETF list — same
# math, one source of truth (compute_vwap resets per session, exactly what a fade
# needs).
from hft_scanner import (
    fetch_intraday, compute_vwap, compute_rsi, compute_ema,
    DEFAULT_INTERVAL, INVERSE_ETF_LIST,
)

log = get_logger("vwap_fade_scanner")


# ─── Master switch ──────────────────────────────────────────────────────────────
# DARK until backtest_vwap_fade.py passes on two independent samples. While False,
# run_vwap_fade_scan() returns [] and the executor loop idles — the whole strategy
# is a no-op, safe to register in start_all.py.
ENABLE_VWAP_FADE = False


# ─── Signal config ──────────────────────────────────────────────────────────────

# Stretch: volatility-normalized distance from VWAP.
STRETCH_STDEV_WINDOW = 14     # bars of rolling stdev of (Close − VWAP), per session
STRETCH_MIN_PERIODS  = 6      # need this many bars before a band is meaningful
Z_STRETCH_MIN        = 2.0    # |z| >= 2σ from VWAP = "stretched"
Z_HIGH_CONVICTION    = 2.5    # |z| >= this (+ RSI extreme) = high-conviction fade
MIN_STRETCH_PCT      = 0.35   # AND >= 0.35% of price (abs floor vs a flat name's noise)

# Momentum extreme (RSI-9, matching the repo's intraday cadence).
FADE_RSI_WINDOW      = 9
RSI_OVERBOUGHT       = 80.0   # PUT-fade side (stretched ABOVE VWAP)
RSI_OVERSOLD         = 20.0   # CALL-fade side (stretched BELOW VWAP)
RSI_HIGH_CONV_OB     = 85.0   # high-conviction RSI extremes
RSI_HIGH_CONV_OS     = 15.0

# Trend-day skip (the killer gate). A fade is skipped when the day trends in the
# direction OPPOSING the fade, measured three independent ways (ANY trips it).
# CRITICAL: the efficiency-ratio / net-direction context EXCLUDES the last
# IMPULSE_BARS — the sharp move we're fading. Otherwise the very flush that makes
# RSI extreme would itself register as "trend" and reject every fade. We instead
# ask "was the market ALREADY trending against us BEFORE this exhaustion move?".
IMPULSE_BARS         = 5      # bars the stretch move spans: the RSI-extreme lookback
                              # AND the span excluded from the trend context (so the
                              # flush/rip we fade isn't itself counted as "trend")
ER_WINDOW            = 20     # Kaufman efficiency-ratio lookback (0=chop, 1=clean trend)
ER_TREND_MAX         = 0.45   # ER >= this, opposing the fade -> trend day -> SKIP
SESSION_SIDE_MAX     = 0.85   # >= 85% of today's bars on the stretched side of VWAP -> SKIP
EMA_SLOPE_LOOKBACK   = 20     # bars for the EMA20 slope measure
EMA_SLOPE_PCT_MAX    = 0.60   # |EMA20 slope| >= 0.60% of price, opposing -> SKIP
TREND_EMA_WINDOW     = 20

MIN_BARS             = 25     # need enough history for the bands / ER / slope

# Liquidity — tighter than HFT (fades need tight spreads + fast fills).
MIN_PRICE            = 10.0
MIN_AVG_VOLUME       = 2_000_000
MIN_DOLLAR_VOLUME    = 50_000_000
UNIVERSE_LIMIT       = 250

# Session window — start after the opening trend (where "far from VWAP" is normal
# and VWAP itself is unstable) and stop early enough that the 30-min time-stop
# still lands before the EOD flatten. Tighter than HFT's 9:45–14:45.
FADE_SESSION_START_ET = datetime.time(10, 0)
FADE_SESSION_END_ET   = datetime.time(14, 30)


# ─── Indicators (new pure helpers — none of these exist elsewhere in the repo) ──

def compute_vwap_bands(df: pd.DataFrame, vwap: pd.Series,
                       window: int = STRETCH_STDEV_WINDOW) -> pd.Series:
    """
    Rolling stdev of (Close − VWAP), reset PER SESSION (same per-day grouping as
    compute_vwap, so a fetch that spans two sessions doesn't blend them). This is
    the denominator that turns raw distance-from-VWAP into a comparable z-score.
    """
    dev = df["Close"] - vwap
    day = df.index.date
    return dev.groupby(day).transform(
        lambda s: s.rolling(window, min_periods=STRETCH_MIN_PERIODS).std()
    )


def vwap_stretch_z(df: pd.DataFrame, vwap: pd.Series) -> float:
    """
    Signed z-score of the last bar's distance from VWAP (+ = above, − = below).
    0.0 when the band isn't defined yet (fail-safe — no signal).
    """
    band = compute_vwap_bands(df, vwap)
    last_dev  = float(df["Close"].iloc[-1] - vwap.iloc[-1])
    last_band = float(band.iloc[-1])
    if not np.isfinite(last_band) or last_band <= 0:
        return 0.0
    return last_dev / last_band


def efficiency_ratio(close: pd.Series, window: int = ER_WINDOW) -> float:
    """
    Kaufman efficiency ratio over `window` bars: |net move| / Σ|bar-to-bar moves|.
    ~1.0 = a clean one-way trend, ~0.0 = chop. The repo has no ADX; this is the
    pure, cheap trend-strength substitute.
    """
    seg = close.tail(window + 1)
    if len(seg) < 2:
        return 0.0
    net  = abs(float(seg.iloc[-1] - seg.iloc[0]))
    path = float(seg.diff().abs().sum())
    return net / path if path > 0 else 0.0


def is_reversal_bar(df: pd.DataFrame, side: str) -> bool:
    """
    True when the last bar shows the stretch STALLING/REVERSING (not accelerating)
    — the difference between an exhausted rip and the first leg of a trend day.

    side="put"  (fading a rip up):   close in lower half of range, OR a down bar,
                                     OR a lower high than the prior bar.
    side="call" (fading a flush down): close in upper half, OR an up bar,
                                     OR a higher low than the prior bar.
    """
    if len(df) < 2:
        return False
    last, prev = df.iloc[-1], df.iloc[-2]
    rng = float(last["High"] - last["Low"])
    if rng <= 0:
        return False
    close_pos = (float(last["Close"]) - float(last["Low"])) / rng
    if side == "put":
        return (close_pos <= 0.5
                or float(last["Close"]) < float(last["Open"])
                or float(last["High"]) <= float(prev["High"]))
    return (close_pos >= 0.5
            or float(last["Close"]) > float(last["Open"])
            or float(last["Low"]) >= float(prev["Low"]))


def is_trend_day(df: pd.DataFrame, vwap: pd.Series, fade_direction: str) -> bool:
    """
    THE KILLER GATE. True (⇒ skip the fade) when the day is trending in the
    direction that OPPOSES the fade — because that is exactly when a fade gets run
    over. A "bullish" fade (buy calls, fading a flush DOWN) is opposed by a
    downtrend; a "bearish" fade (buy puts, fading a rip UP) is opposed by an
    uptrend. Trips if ANY of three independent measures fires. Fails SAFE: on any
    error it returns True (skip) — a missed fade is cheaper than one into a trend.
    """
    try:
        close = df["Close"]
        price = float(close.iloc[-1])
        if price <= 0 or len(close) <= ER_WINDOW + IMPULSE_BARS:
            return True

        fade_up = fade_direction == "bullish"   # buying calls -> opposing trend is DOWN

        # 1) Efficiency ratio of the PRE-IMPULSE context (exclude the flush/rip we
        #    are fading, else the trigger move itself reads as trend).
        context = close.iloc[:-IMPULSE_BARS]
        er  = efficiency_ratio(context, ER_WINDOW)
        net = float(context.iloc[-1] - context.iloc[-1 - ER_WINDOW])
        trend_up = net > 0
        opposing_trend = (not trend_up) if fade_up else trend_up
        if er >= ER_TREND_MAX and opposing_trend:
            return True

        # 2) Persistent one-side-of-VWAP session (this trading day only).
        today = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index.date[-1]
        mask  = np.array([d == today for d in df.index.date])
        above_frac = float((close[mask] > vwap[mask]).mean()) if mask.any() else 0.5
        # Fading a rip (bearish): a session stuck ABOVE vwap is a persistent uptrend.
        # Fading a flush (bullish): a session stuck BELOW vwap is a persistent downtrend.
        if fade_up and (1.0 - above_frac) >= SESSION_SIDE_MAX:
            return True
        if (not fade_up) and above_frac >= SESSION_SIDE_MAX:
            return True

        # 3) Steep EMA20 slope of the PRE-IMPULSE context (measure the slope
        #    ending just before the flush, so the flush itself doesn't create the
        #    "trend" that rejects the fade — same reasoning as the ER context).
        ema = compute_ema(close, TREND_EMA_WINDOW)
        anchor = len(ema) - 1 - IMPULSE_BARS
        if anchor - EMA_SLOPE_LOOKBACK >= 0:
            slope_pct = (float(ema.iloc[anchor] - ema.iloc[anchor - EMA_SLOPE_LOOKBACK]) / price) * 100
            if fade_up and slope_pct <= -EMA_SLOPE_PCT_MAX:      # steep downtrend
                return True
            if (not fade_up) and slope_pct >= EMA_SLOPE_PCT_MAX:  # steep uptrend
                return True

        return False
    except Exception:
        return True   # fail SAFE — skip the fade if the trend read breaks


def in_fade_session(bar_time: pd.Timestamp) -> bool:
    """True when the bar is inside the fade window (10:00–14:30 ET). Fails open."""
    try:
        t = bar_time.time()
        return FADE_SESSION_START_ET <= datetime.time(t.hour, t.minute, t.second) <= FADE_SESSION_END_ET
    except Exception:
        return True


# ─── Detector ───────────────────────────────────────────────────────────────────

def _score_fade(z: float, rsi: float, side: str) -> int:
    """0–100 from stretch magnitude + RSI extremity (reversal bar is a gate, not
    a score input). Bounded and monotonic in "how exhausted"."""
    stretch_pts = min(45.0, 25.0 + (abs(z) - Z_STRETCH_MIN) * 20.0)   # 25 at floor → 45
    if side == "put":
        rsi_pts = min(35.0, max(0.0, (rsi - RSI_OVERBOUGHT)) * 3.0 + 15.0)
    else:
        rsi_pts = min(35.0, max(0.0, (RSI_OVERSOLD - rsi)) * 3.0 + 15.0)
    return int(round(min(100.0, stretch_pts + rsi_pts + 20.0)))   # +20 base for a valid setup


def detect_vwap_fade(df: pd.DataFrame, vwap: pd.Series) -> dict:
    """
    Evaluate the fade on a single ticker's intraday frame.

    Returns {signal, direction, score, conviction, detail, z, rsi}. `direction`
    is "bearish" (→PUT, fade a rip) or "bullish" (→CALL, fade a flush).
    """
    none = {"signal": False, "direction": None, "score": 0,
            "conviction": "relaxed", "detail": "", "z": 0.0, "rsi": float("nan")}
    if len(df) < MIN_BARS:
        return none

    z = vwap_stretch_z(df, vwap)
    price     = float(df["Close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1])
    if price <= 0 or last_vwap <= 0:
        return none
    pct_vs_vwap = (price - last_vwap) / last_vwap * 100

    # Gate 1: genuinely stretched (z AND absolute %).
    if abs(z) < Z_STRETCH_MIN or abs(pct_vs_vwap) < MIN_STRETCH_PCT:
        return none

    rsi_series = compute_rsi(df["Close"], FADE_RSI_WINDOW)
    rsi = float(rsi_series.iloc[-1])
    # Momentum EXTREME reached during the impulse — not necessarily on the current
    # bar, since the stall/reversal bar we require ticks RSI back off its extreme.
    # We fade exhaustion that just happened, confirmed by the stall.
    window = rsi_series.tail(IMPULSE_BARS)
    rsi_hi = float(window.max())
    rsi_lo = float(window.min())
    if not (np.isfinite(rsi_hi) and np.isfinite(rsi_lo)):
        return none

    # Gate 2: momentum extreme on the correct side, + a stall/reversal bar.
    if z > 0:                                    # above VWAP -> fade DOWN with a put
        if rsi_hi < RSI_OVERBOUGHT or not is_reversal_bar(df, "put"):
            return none
        direction, side, rsi_ext = "bearish", "put", rsi_hi
    else:                                        # below VWAP -> fade UP with a call
        if rsi_lo > RSI_OVERSOLD or not is_reversal_bar(df, "call"):
            return none
        direction, side, rsi_ext = "bullish", "call", rsi_lo

    # Gate 3: the killer — never fade into a trend day.
    if is_trend_day(df, vwap, direction):
        return {**none, "detail": f"skip: trend day (z={z:.1f}, rsi={rsi:.0f})"}

    score = _score_fade(z, rsi_ext, side)
    high = abs(z) >= Z_HIGH_CONVICTION and (
        (side == "put" and rsi_ext >= RSI_HIGH_CONV_OB) or
        (side == "call" and rsi_ext <= RSI_HIGH_CONV_OS))
    conviction = "high" if high else "relaxed"
    detail = (f"{'rip' if side == 'put' else 'flush'} fade: {pct_vs_vwap:+.2f}% vs VWAP "
              f"(z={z:+.1f}), RSI{FADE_RSI_WINDOW} ext={rsi_ext:.0f} now={rsi:.0f}")
    return {"signal": True, "direction": direction, "score": score,
            "conviction": conviction, "detail": detail,
            "z": round(z, 2), "rsi": round(rsi_ext, 1)}


# ─── Per-ticker scan ────────────────────────────────────────────────────────────

def scan_ticker(ticker: str, interval: str = DEFAULT_INTERVAL) -> dict | None:
    """Run the fade detector for one ticker. Returns a setup dict or None."""
    if ticker.upper() in INVERSE_ETF_LIST:
        return None

    df = fetch_intraday(ticker, interval=interval)
    if df.empty or len(df) < MIN_BARS:
        return None

    if not in_fade_session(df.index[-1]):
        return None

    vwap = compute_vwap(df)
    sig  = detect_vwap_fade(df, vwap)
    if not sig["signal"]:
        return None

    last      = df.iloc[-1]
    last_vwap = float(vwap.iloc[-1])
    return {
        "ticker":         ticker,
        "setup_score":    sig["score"],
        "trade_style":    "day",
        "day_trade_ok":   True,
        "strategy":       "vwap_fade",
        "style_reason":   f"day: {sig['conviction']} conviction {sig['direction']} VWAP fade ({sig['detail']})",
        "conviction":     sig["conviction"],
        "direction":      sig["direction"],
        "last_price":     round(float(last["Close"]), 2),
        "vwap":           round(last_vwap, 2),
        "pct_vs_vwap":    round((float(last["Close"]) - last_vwap) / last_vwap * 100, 2),
        "stretch_z":      sig["z"],
        "rsi":            sig["rsi"],
        "interval":       interval,
        "active_signals": ["vwap_fade"],
        "signal_details": {"vwap_fade": sig["detail"]},
        "timestamp":      df.index[-1].isoformat(),
    }


def run_vwap_fade_scan(
    csv_path: str | None = "sp500.csv",
    universe_limit: int = UNIVERSE_LIMIT,
    interval: str = DEFAULT_INTERVAL,
    min_score: int = 0,
    rotation_key: str | None = "vwap_fade",
) -> list[dict]:
    """
    Full VWAP-fade scan. Returns setups sorted by score desc.

    Returns [] immediately while ENABLE_VWAP_FADE is False — the strategy ships
    dark until its backtest validates on two independent samples.
    """
    if not ENABLE_VWAP_FADE:
        log.info("vwap_fade disabled (ENABLE_VWAP_FADE=False) — no scan")
        return []

    print("\n" + "=" * 60)
    print("  VWAP MEAN-REVERSION FADE SCANNER  —  Strategy 6")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  interval={interval}")
    print("=" * 60 + "\n")

    symbols = load_universe(csv_path=csv_path, limit=universe_limit, rotation_key=rotation_key)
    liquid  = filter_universe(
        symbols=symbols,
        min_price=MIN_PRICE,
        min_avg_volume=MIN_AVG_VOLUME,
        min_avg_dollar_volume=MIN_DOLLAR_VOLUME,
    )
    print(f"[VWAP-Fade] {len(liquid)} liquid symbols to scan\n")

    results = []
    for ticker in liquid:
        setup = scan_ticker(ticker, interval=interval)
        if setup and setup["setup_score"] >= min_score:
            results.append(setup)
            print(f"[VWAP-Fade] ✅ {ticker} | Score: {setup['setup_score']}/100 | "
                  f"Dir: {setup['direction']} | {setup['signal_details']['vwap_fade']}")

    results.sort(key=lambda x: x["setup_score"], reverse=True)
    print("\n" + "=" * 60)
    print(f"  VWAP-FADE SETUPS FOUND: {len(results)}")
    print("=" * 60)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VWAP Mean-Reversion Fade Scanner — Strategy 6")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--limit", type=int, default=UNIVERSE_LIMIT)
    args = parser.parse_args()
    found = run_vwap_fade_scan(universe_limit=args.limit, interval=args.interval)
    if not found and not ENABLE_VWAP_FADE:
        print("\n[VWAP-Fade] Strategy is DARK (ENABLE_VWAP_FADE=False). "
              "Validate via backtest_vwap_fade.py, then flip the flag.")
