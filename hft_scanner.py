"""
hft_scanner.py  —  Strategy 3: Intraday Momentum Scanner

Scans for high-probability short-term directional moves using
intraday price and volume data. Targets 0–1 DTE options for
quick scalps with 15–60 minute hold times.

Signals detected:
    1. VWAP Reclaim    — price crosses above VWAP after a dip, with vol surge
    2. Opening Range Breakout (ORB) — price breaks the first 15-min high/low
    3. Volume Spike    — sudden vol >= 3x rolling average on up-bar
    4. Momentum Burst  — short-term ROC + fast RSI(9) confirmation
    5. Range Breakout  — break of last N-bar consolidation range
    6. VWAP Bounce     — trend pullback that holds VWAP as support
    7. Strong Bar      — close near the bar's extreme (price action conviction)

Each signal is scored 0–100. Only setups >= MIN_SIGNAL_SCORE are returned.

Usage:
    from hft_scanner import run_hft_scan
    setups = run_hft_scan()

    # CLI
    python hft_scanner.py
    python hft_scanner.py --interval 1m --limit 50
"""

import datetime
import math
import time
import argparse

import numpy as np
import pandas as pd

from universe import load_universe
from market_filter import filter_universe
from market_data import get_intraday_bars
from logger import get_logger

log = get_logger("hft_scanner")


# ─── Config ────────────────────────────────────────────────────────────────────

DEFAULT_INTERVAL    = "5m"      # Bar size: "1m", "2m", "5m"
DEFAULT_PERIOD      = "1d"      # How far back to pull (intraday max 7d)
ORB_MINUTES         = 15        # Opening range window in minutes
VWAP_VOL_MULT       = 2.0       # Min vol multiplier for VWAP reclaim signal
SPIKE_VOL_MULT      = 3.0       # Min vol multiplier for raw spike signal
MIN_SIGNAL_SCORE    = 50        # Minimum score to include in output (45→50 Jun 30 2026 — reverted the frequency push; theta-honest 2-sample backtest validated 50)
UNIVERSE_LIMIT      = 250       # Symbols to scan per cycle (wider = more setups)
MIN_PRICE           = 5.0       # Skip penny stocks
MIN_AVG_VOLUME      = 1_000_000 # Liquidity floor (daily avg shares)
MIN_DOLLAR_VOLUME   = 20_000_000   # cut micro-caps / thin names (matches the universe screen)

# How many CORE directional signals must fire together to qualify a setup.
# The floor runs over the 5 core signals (vwap, orb, spike, range, bounce).
#   History: the original strategy hard-required all THREE of VWAP+ORB+Spike.
#   Jun 2026 backtests: a floor of 3 was positive on two independent samples
#   (PF ~1.6); a floor of 2 was positive on one sample but negative on the
#   other (mixed evidence, not strictly a loser).
#   Jun 10 2026: floor lowered to 2 for trade frequency (at 3 the scanner found
#   ~1 setup/day). But live results at 2 were negative (15% win, theta bleed on
#   0-DTE), and the Jun 30 THETA-HONEST backtest (full BS pricing, real time
#   decay) showed 2/45 is OVERFIT: +$996 on mega-caps but -$219 on a broad
#   basket, driven by unreliable "relaxed" (non-proven-trio) trades. At 3/50 the
#   strategy is positive on BOTH samples (mega +$747 PF 1.51 / broad +$238 PF
#   1.12). So frequency was a false economy — floor restored to 3.
#   Tradeoff accepted: 3 trades less often but with a real, robust edge.
# RE-RUN backtest_hft.py after changing this to validate.
HFT_MIN_CONFLUENCE  = 3
RANGE_LOOKBACK      = 12        # bars for the intraday range-breakout trigger (12×5m = 1h)

# Bidirectional (long + short) signal generation.
# Four of the core detectors (vwap reclaim, volume spike, momentum burst, vwap
# bounce) historically fired LONG only, giving the strategy a structural long
# bias and far fewer setups in down-trending sessions. When this is True they
# also emit the mirror-image BEARISH signal (VWAP rejection, distribution spike,
# down-momentum, VWAP resistance-reject), direction resolution becomes a weighted
# vote across all directional signals, and composite scoring only counts a signal
# toward a setup when its direction agrees.
#
# DEFAULT FALSE so the live path is unchanged until validated. Flip to True only
# after backtest_hft.py --bidirectional confirms it isn't worse than long-only.
ENABLE_BIDIRECTIONAL_SIGNALS = False

# ── Session gate ───────────────────────────────────────────────────────────────
# Only fire signals during "prime time" — avoids opening noise and EOD entries
# that carry overnight and gap against us.  Times are US/Eastern (ET).
# Tradier timesales returns timezone-naive ET timestamps; compare directly.
# 9:45 AM ET:  the 15-min opening range has just completed — this is exactly
#              when ORB breakouts trigger, so starting later (the old 10:00)
#              missed the strategy's best morning-momentum entries.
# 2:45 PM ET:  leaves >=30 min before the EOD flatten (3:15) for a position to
#              develop. Entries after this carry too little runway.
# Widened from 10:00–14:30 (Jun 2026) as part of the trade-frequency push.
PRIME_SESSION_START_ET = datetime.time(9, 45)   # 9:45 AM ET
PRIME_SESSION_END_ET   = datetime.time(14, 45)  # 2:45 PM ET

# Inverse / leveraged-inverse ETFs: bullish momentum signals are structurally
# wrong on these instruments.  The executor will skip any ticker on this list.
INVERSE_ETF_LIST = {
    "SQQQ", "SDOW", "SPXS", "SPXU", "TECS", "FAZ", "LABD",
    "SOXS", "UVXY", "SVXY", "VIXY", "SDS", "PSQ", "DOG",
}

# EMA window used for trend alignment filter
TREND_EMA_WINDOW = 20

# RSI window for the momentum burst signal.  9 reacts faster than the
# standard 14 — appropriate for intraday scalp timeframes.
RSI_FAST_WINDOW = 9


# ─── Data Fetching ─────────────────────────────────────────────────────────────

# Bar interval → seconds, used to size the intraday fetch cache TTL.
_INTERVAL_SECONDS = {
    "1m": 60, "1min": 60,
    "2m": 120,
    "5m": 300, "5min": 300,
    "15m": 900, "15min": 900,
}

# Per-(ticker, interval) cache of the last fetched intraday DataFrame.
# The HFT loop re-scans every SCAN_INTERVAL_SEC (60s), but a 5m bar only changes
# every 300s — so 4 of every 5 fetches used to return the SAME bars and recompute
# identical signals. We reuse a cached fetch until ~80% of the bar interval has
# elapsed, cutting intraday API calls ~5x while still refreshing before each new
# bar prints. Module-level (per-process); the backtest never calls fetch_intraday.
_intraday_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}


def _intraday_cache_ttl(interval: str) -> float:
    """Reuse a cached fetch for ~80% of the bar interval (so we refresh just
    before the next bar appears rather than once per 60s loop)."""
    return _INTERVAL_SECONDS.get(interval, 300) * 0.8


def clear_intraday_cache() -> None:
    """Drop all cached intraday fetches (e.g. on a new session or in tests)."""
    _intraday_cache.clear()


def fetch_intraday(ticker: str, interval: str = DEFAULT_INTERVAL,
                   period: str = DEFAULT_PERIOD) -> pd.DataFrame:
    """
    Pull intraday OHLCV bars via Tradier timesales, with a short TTL cache.

    A cached DataFrame is reused while it is still fresh for the bar interval
    (see _intraday_cache_ttl) so repeated scan cycles don't re-download bars
    that haven't changed. Empty results are never cached (transient failure /
    closed market) so they can be retried immediately.

    Returns a clean DataFrame with columns: Open, High, Low, Close, Volume.
    Returns empty DataFrame on any failure or when market is closed.
    """
    key = (ticker, interval)
    cached = _intraday_cache.get(key)
    if cached is not None and (time.time() - cached[0]) < _intraday_cache_ttl(interval):
        return cached[1]

    # Convert period string to calendar days (default "1d" → 1 day)
    days_map = {"1d": 1, "2d": 2, "5d": 5, "1wk": 7}
    days = days_map.get(period, 1)
    df = get_intraday_bars(ticker, interval=interval, days=days)
    if df.empty:
        log.debug("No intraday data for %s (%s)", ticker, interval)
        return df

    _intraday_cache[key] = (time.time(), df)
    return df


# ─── Indicators ────────────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Intraday VWAP, reset per trading day.

    The cumulative sums are grouped by calendar date because fetch_intraday's
    1-day window actually spans TWO sessions on Tue–Fri (it starts at
    yesterday 9:30). A plain cumsum() would anchor today's VWAP at yesterday's
    open — a different line than the per-session VWAP the strategy was
    backtested on (backtest_hft slices per session for exactly this reason).
    """
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    day = df.index.date
    cum_tpv = (typical * df["Volume"]).groupby(day).cumsum()
    cum_vol = df["Volume"].groupby(day).cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=window - 1, min_periods=window).mean()
    avg_l = loss.ewm(com=window - 1, min_periods=window).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_rolling_vol_avg(volume: pd.Series, window: int = 20) -> pd.Series:
    return volume.rolling(window, min_periods=5).mean()


def compute_ema(close: pd.Series, window: int = TREND_EMA_WINDOW) -> pd.Series:
    return close.ewm(span=window, adjust=False).mean()


def is_prime_session(bar_time: pd.Timestamp) -> bool:
    """
    Returns True when the bar falls inside the prime intraday window.

    Tradier timesales returns US/Eastern timestamps (timezone-naive in the
    DataFrame).  Compare directly against ET boundaries — no UTC conversion.
    Prime window: PRIME_SESSION_START_ET – PRIME_SESSION_END_ET
    (currently 9:45 AM – 2:45 PM ET).

    Bars outside this window are:
      - Opening-range formation noise            (before the start)
      - EOD entries with too little runway left  (after the end)
    """
    try:
        t = bar_time.time()
        t = datetime.time(t.hour, t.minute, t.second)
        return PRIME_SESSION_START_ET <= t <= PRIME_SESSION_END_ET
    except Exception:
        return True   # Fail open — don't block on timezone edge cases


def detect_trend_alignment(df: pd.DataFrame) -> dict:
    """
    Checks whether price is trending in a clear direction using EMA(20).

    Bullish alignment:  close > EMA AND close > VWAP-ish (last 5 bars rising)
    Bearish alignment:  close < EMA AND close falling

    Returns:
        {
            "aligned_bullish": bool,
            "aligned_bearish": bool,
            "score_bonus":     int   (+15 if aligned with direction, -10 if counter)
            "detail":          str
        }
    """
    if len(df) < TREND_EMA_WINDOW + 2:
        return {"aligned_bullish": False, "aligned_bearish": False,
                "score_bonus": 0, "detail": "insufficient bars for EMA"}

    close = df["Close"].astype(float)
    ema   = compute_ema(close)

    last_close = float(close.iloc[-1])
    last_ema   = float(ema.iloc[-1])
    prev_ema   = float(ema.iloc[-3])

    ema_rising  = last_ema > prev_ema
    ema_falling = last_ema < prev_ema

    above_ema = last_close > last_ema
    below_ema = last_close < last_ema

    aligned_bullish = above_ema and ema_rising
    aligned_bearish = below_ema and ema_falling

    pct_vs_ema = round((last_close - last_ema) / last_ema * 100, 2) if last_ema > 0 else 0

    detail = (
        f"EMA({TREND_EMA_WINDOW})={last_ema:.2f} | "
        f"price {'above' if above_ema else 'below'} EMA by {abs(pct_vs_ema):.2f}% | "
        f"EMA {'rising' if ema_rising else 'falling'}"
    )

    return {
        "aligned_bullish": aligned_bullish,
        "aligned_bearish": aligned_bearish,
        "score_bonus":     0,   # Applied in score_hft_setup based on direction
        "detail":          detail,
        "pct_vs_ema":      pct_vs_ema,
    }


def get_opening_range(df: pd.DataFrame, orb_minutes: int = ORB_MINUTES
                      ) -> tuple[float, float] | None:
    """
    Returns (orb_high, orb_low) from the first `orb_minutes` of today's session.
    Returns None if there is not enough data.
    """
    today = df.index[-1].date()
    today_bars = df[df.index.date == today]
    if today_bars.empty:
        return None

    session_start = today_bars.index[0]
    orb_end = session_start + datetime.timedelta(minutes=orb_minutes)
    orb_bars = today_bars[today_bars.index <= orb_end]

    if orb_bars.empty:
        return None

    return float(orb_bars["High"].max()), float(orb_bars["Low"].min())


# ─── Signal Detection ──────────────────────────────────────────────────────────

def detect_vwap_reclaim(df: pd.DataFrame, vwap: pd.Series) -> dict:
    """
    VWAP Reclaim (bullish): last bar closed ABOVE VWAP after a bar below.
    VWAP Rejection (bearish, bidirectional mode): closed BELOW VWAP after a bar
    above. Both require volume >= VWAP_VOL_MULT × rolling average to score full.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < 5:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    close   = df["Close"]
    vol     = df["Volume"]
    vol_avg = compute_rolling_vol_avg(vol)

    last_close = float(close.iloc[-1])
    last_vwap  = float(vwap.iloc[-1])
    prev_close = float(close.iloc[-2])
    prev_vwap  = float(vwap.iloc[-2])
    last_vol   = float(vol.iloc[-1])
    avg_vol    = float(vol_avg.iloc[-1]) if not math.isnan(float(vol_avg.iloc[-1])) else 1

    up_cross   = (prev_close < prev_vwap) and (last_close > last_vwap)
    down_cross = (prev_close > prev_vwap) and (last_close < last_vwap)
    vol_confirmed = (avg_vol > 0) and (last_vol / avg_vol >= VWAP_VOL_MULT)
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0

    if up_cross:
        score = 30
        if vol_confirmed:
            score += 30
        pct = round((last_close - last_vwap) / last_vwap * 100, 2) if last_vwap > 0 else 0
        if pct >= 0.5:
            score += 20
        elif pct >= 0.2:
            score += 10
        return {
            "signal": True, "score": min(score, 80), "direction": "bullish",
            "detail": f"VWAP reclaim | vol {vol_ratio:.1f}x avg | +{pct:.2f}% above VWAP",
        }

    if ENABLE_BIDIRECTIONAL_SIGNALS and down_cross:
        score = 30
        if vol_confirmed:
            score += 30
        pct = round((last_vwap - last_close) / last_vwap * 100, 2) if last_vwap > 0 else 0
        if pct >= 0.5:
            score += 20
        elif pct >= 0.2:
            score += 10
        return {
            "signal": True, "score": min(score, 80), "direction": "bearish",
            "detail": f"VWAP rejection | vol {vol_ratio:.1f}x avg | -{pct:.2f}% below VWAP",
        }

    return {"signal": False, "score": 0, "detail": "no VWAP crossover", "direction": "none"}


def detect_orb_breakout(df: pd.DataFrame) -> dict:
    """
    Opening Range Breakout: price breaks above ORB high on volume.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    orb = get_opening_range(df, ORB_MINUTES)
    if orb is None:
        return {"signal": False, "score": 0, "detail": "no ORB data", "direction": "none"}

    orb_high, orb_low = orb
    last_close = float(df["Close"].iloc[-1])
    last_vol   = float(df["Volume"].iloc[-1])
    vol_avg    = float(compute_rolling_vol_avg(df["Volume"]).iloc[-1])

    # Must be past the ORB window
    today       = df.index[-1].date()
    today_bars  = df[df.index.date == today]
    session_start = today_bars.index[0]
    orb_end       = session_start + datetime.timedelta(minutes=ORB_MINUTES)
    if df.index[-1] <= orb_end:
        return {"signal": False, "score": 0, "detail": "still in ORB window", "direction": "none"}

    vol_ok = (vol_avg > 0) and (last_vol / vol_avg >= 1.5)

    if last_close > orb_high:
        score = 40
        if vol_ok:
            score += 25
        pct_break = round((last_close - orb_high) / orb_high * 100, 2)
        if pct_break >= 0.5:
            score += 15
        return {
            "signal":    True,
            "score":     min(score, 80),
            "direction": "bullish",
            "detail":    f"ORB breakout above ${orb_high:.2f} by +{pct_break:.2f}%",
        }

    if last_close < orb_low:
        score = 40
        if vol_ok:
            score += 20
        pct_break = round((orb_low - last_close) / orb_low * 100, 2)
        return {
            "signal":    True,
            "score":     min(score, 60),
            "direction": "bearish",
            "detail":    f"ORB breakdown below ${orb_low:.2f} by -{pct_break:.2f}%",
        }

    return {"signal": False, "score": 0, "detail": "inside ORB range", "direction": "none"}


def detect_volume_spike(df: pd.DataFrame) -> dict:
    """
    Standalone volume spike (>= SPIKE_VOL_MULT × rolling average):
      bullish — on an UP bar (close > open): accumulation.
      bearish (bidirectional mode) — on a DOWN bar (close < open): distribution.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < 10:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    last  = df.iloc[-1]
    vol   = df["Volume"]
    vol_avg = float(compute_rolling_vol_avg(vol).iloc[-1])

    if math.isnan(vol_avg) or vol_avg <= 0:
        return {"signal": False, "score": 0, "detail": "vol avg unavailable", "direction": "none"}

    vol_ratio   = float(last["Volume"]) / vol_avg
    is_up_bar   = float(last["Close"]) > float(last["Open"])
    is_down_bar = float(last["Close"]) < float(last["Open"])

    if vol_ratio < SPIKE_VOL_MULT:
        return {"signal": False, "score": 0, "direction": "none",
                "detail": f"vol {vol_ratio:.1f}x (need {SPIKE_VOL_MULT}x)"}

    # Volume tier (shared by both directions)
    score = 25
    if vol_ratio >= 5.0:
        score += 25
    elif vol_ratio >= 4.0:
        score += 20
    elif vol_ratio >= 3.0:
        score += 10

    if is_up_bar:
        return {"signal": True, "score": min(score, 60), "direction": "bullish",
                "detail": f"Volume spike {vol_ratio:.1f}x avg on up-bar"}

    if ENABLE_BIDIRECTIONAL_SIGNALS and is_down_bar:
        return {"signal": True, "score": min(score, 60), "direction": "bearish",
                "detail": f"Distribution spike {vol_ratio:.1f}x avg on down-bar"}

    return {"signal": False, "score": 0, "direction": "none",
            "detail": f"vol {vol_ratio:.1f}x on a doji/indecisive bar"}


def detect_momentum_burst(df: pd.DataFrame) -> dict:
    """
    Short-term momentum: 5-bar ROC + fast RSI(9).
      bullish — ROC > 0 and RSI rising in the 50–80 zone.
      bearish (bidirectional mode) — ROC < 0 and RSI falling in the 20–50 zone.

    Uses RSI_FAST_WINDOW (9) instead of the classic 14 so the indicator
    reacts quickly enough to be useful on a scalp timeframe.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < RSI_FAST_WINDOW + 5:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    close = df["Close"].astype(float)
    rsi   = compute_rsi(close, window=RSI_FAST_WINDOW)

    rsi_now  = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-5])

    roc_5 = (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100

    rsi_rising  = rsi_now > rsi_prev
    rsi_falling = rsi_now < rsi_prev

    def _roc_score(magnitude: float) -> int:
        if magnitude >= 2.0:
            return 30
        if magnitude >= 1.0:
            return 20
        if magnitude >= 0.5:
            return 10
        return 0

    # Bullish: rising momentum. RSI ceiling 80 (was 75) — strong intraday trends
    # often stay overbought; capping at 75 filtered out the best continuations.
    if rsi_rising and roc_5 > 0:
        score = _roc_score(roc_5)
        score += 20 if (50 <= rsi_now <= 80) else 10
        return {
            "signal": score >= 20, "score": min(score, 50), "direction": "bullish",
            "detail": f"ROC5={roc_5:.2f}% | RSI({RSI_FAST_WINDOW})={rsi_now:.1f} (↑)",
        }

    # Bearish: falling momentum (mirror of the bullish zone, 20–50).
    if ENABLE_BIDIRECTIONAL_SIGNALS and rsi_falling and roc_5 < 0:
        score = _roc_score(abs(roc_5))
        score += 20 if (20 <= rsi_now <= 50) else 10
        return {
            "signal": score >= 20, "score": min(score, 50), "direction": "bearish",
            "detail": f"ROC5={roc_5:.2f}% | RSI({RSI_FAST_WINDOW})={rsi_now:.1f} (↓)",
        }

    return {"signal": False, "score": 0, "direction": "none",
            "detail": f"ROC={roc_5:.2f}% RSI={rsi_now:.1f} rising={rsi_rising}"}


def detect_range_breakout(df: pd.DataFrame, lookback: int = RANGE_LOOKBACK) -> dict:
    """
    Intraday range breakout: price breaks above the high (or below the low) of
    the last `lookback` bars on a volume confirmation. Distinct from ORB — this
    fires any time of day off a fresh consolidation, not just the opening range.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < lookback + 2:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    prior      = df.iloc[-(lookback + 1):-1]       # the `lookback` bars before the current one
    range_high = float(prior["High"].max())
    range_low  = float(prior["Low"].min())
    last_close = float(df["Close"].iloc[-1])
    last_vol   = float(df["Volume"].iloc[-1])
    vol_avg    = float(compute_rolling_vol_avg(df["Volume"]).iloc[-1])
    vol_ok     = (vol_avg > 0) and (last_vol / vol_avg >= 1.5)

    if last_close > range_high:
        score = 30 + (25 if vol_ok else 0)
        pct = round((last_close - range_high) / range_high * 100, 2)
        return {"signal": True, "score": min(score, 70), "direction": "bullish",
                "detail": f"Range breakout above ${range_high:.2f} (+{pct:.2f}%)"}
    if last_close < range_low:
        score = 30 + (20 if vol_ok else 0)
        pct = round((range_low - last_close) / range_low * 100, 2)
        return {"signal": True, "score": min(score, 60), "direction": "bearish",
                "detail": f"Range breakdown below ${range_low:.2f} (-{pct:.2f}%)"}
    return {"signal": False, "score": 0, "detail": "inside range", "direction": "none"}


def detect_vwap_bounce(df: pd.DataFrame, vwap: pd.Series) -> dict:
    """
    VWAP pullback continuation (a trend entry, not a cross like VWAP reclaim):
      bullish — price was above VWAP, dipped to test it as SUPPORT, holds and
                turns back up.
      bearish (bidirectional mode) — price was below VWAP, rallied to test it as
                RESISTANCE, fails and turns back down.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < 6:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    close      = df["Close"].astype(float)
    last_close = float(close.iloc[-1])
    last_vwap  = float(vwap.iloc[-1])
    prev_close = float(close.iloc[-2])
    if last_vwap <= 0:
        return {"signal": False, "score": 0, "detail": "no vwap", "direction": "none"}

    vol_avg  = float(compute_rolling_vol_avg(df["Volume"]).iloc[-1])
    last_vol = float(df["Volume"].iloc[-1])
    vol_ok   = (vol_avg > 0) and (last_vol / vol_avg >= 1.2)

    # Bullish: was above, dipped to test support, turned back up holding above.
    was_above   = float(close.iloc[-5]) > float(vwap.iloc[-5]) * 1.002
    recent_low  = float(df["Low"].iloc[-4:-1].min())
    held_support = (recent_low - last_vwap) / last_vwap <= 0.003 and recent_low >= last_vwap * 0.995
    turned_up    = last_close > last_vwap and last_close > prev_close

    if was_above and held_support and turned_up:
        score = 35 + (15 if vol_ok else 0)
        return {"signal": True, "score": min(score, 55), "direction": "bullish",
                "detail": f"VWAP bounce — held ${last_vwap:.2f} support, turning up"}

    # Bearish mirror: was below, rallied to test resistance, turned back down.
    if ENABLE_BIDIRECTIONAL_SIGNALS:
        was_below      = float(close.iloc[-5]) < float(vwap.iloc[-5]) * 0.998
        recent_high    = float(df["High"].iloc[-4:-1].max())
        held_resist    = (last_vwap - recent_high) / last_vwap <= 0.003 and recent_high <= last_vwap * 1.005
        turned_down    = last_close < last_vwap and last_close < prev_close
        if was_below and held_resist and turned_down:
            score = 35 + (15 if vol_ok else 0)
            return {"signal": True, "score": min(score, 55), "direction": "bearish",
                    "detail": f"VWAP reject — failed ${last_vwap:.2f} resistance, turning down"}

    return {"signal": False, "score": 0, "detail": "no VWAP bounce", "direction": "none"}


def detect_strong_bar(df: pd.DataFrame) -> dict:
    """
    Price-action conviction: measures where the current bar closed within its
    high–low range.  A close near the extreme shows that buyers/sellers were in
    control for the whole bar — higher-quality signal than price crossing a line.

    Bullish: close in the top 70%+ of the bar's range (strong demand).
    Bearish: close in the bottom 30% or less (strong supply).
    Volume must be >= 1.2× the rolling average to confirm conviction.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    if len(df) < 5:
        return {"signal": False, "score": 0, "detail": "insufficient bars", "direction": "none"}

    last      = df.iloc[-1]
    high      = float(last["High"])
    low       = float(last["Low"])
    close     = float(last["Close"])
    bar_range = high - low

    if bar_range <= 0:
        return {"signal": False, "score": 0, "detail": "zero-range bar", "direction": "none"}

    bar_quality = (close - low) / bar_range

    vol_avg  = float(compute_rolling_vol_avg(df["Volume"]).iloc[-1])
    last_vol = float(last["Volume"])
    vol_ok   = (vol_avg > 0) and (last_vol / vol_avg >= 1.2)

    if bar_quality >= 0.70:
        score = 25
        if bar_quality >= 0.85:
            score += 10     # extra-strong close
        if vol_ok:
            score += 15     # volume-confirmed conviction
        return {
            "signal":    True,
            "score":     min(score, 50),
            "direction": "bullish",
            "detail":    (f"Strong bull bar: close {bar_quality:.0%} of range"
                          + (" +vol" if vol_ok else "")),
        }

    if bar_quality <= 0.30:
        score = 25
        if bar_quality <= 0.15:
            score += 10
        if vol_ok:
            score += 15
        return {
            "signal":    True,
            "score":     min(score, 50),
            "direction": "bearish",
            "detail":    (f"Strong bear bar: close {bar_quality:.0%} of range"
                          + (" +vol" if vol_ok else "")),
        }

    return {
        "signal":    False,
        "score":     0,
        "detail":    f"Indecisive bar: close {bar_quality:.0%} of range",
        "direction": "none",
    }


# ─── Composite Scoring ─────────────────────────────────────────────────────────

def _aligned_score(sig: dict, direction: str) -> int:
    """
    The score a signal contributes to a setup of the given direction.

    In bidirectional mode a directional signal only counts when its own
    direction agrees with the setup (or it is directionless) — a bearish signal
    never pads a bullish setup and vice-versa. In long-only (legacy) mode every
    signal's score counts regardless of direction, exactly as before.
    """
    score = sig.get("score", 0)
    if not ENABLE_BIDIRECTIONAL_SIGNALS:
        return score
    return score if sig.get("direction", "none") in (direction, "none") else 0


def resolve_direction(signals: dict) -> str:
    """
    Decide a setup's trade direction from its signals.

    Long-only (legacy) mode: ORB direction wins, then range breakout, else
    default bullish — exactly the original behavior.

    Bidirectional mode: a score-weighted vote across every directional signal;
    the heavier side wins, ties default bullish (preserves the prior long bias
    only as a tiebreaker).
    """
    if not ENABLE_BIDIRECTIONAL_SIGNALS:
        orb_dir   = signals.get("orb", {}).get("direction", "none")
        range_dir = signals.get("range", {}).get("direction", "none")
        if orb_dir in ("bullish", "bearish"):
            return orb_dir
        if range_dir in ("bullish", "bearish"):
            return range_dir
        return "bullish"

    bull = bear = 0.0
    for name, s in signals.items():
        if name == "trend" or not isinstance(s, dict):
            continue
        d = s.get("direction", "none")
        if d == "bullish":
            bull += s.get("score", 0)
        elif d == "bearish":
            bear += s.get("score", 0)
    return "bearish" if bear > bull else "bullish"


def hft_conviction(signals: dict, direction: str = "bullish") -> str:
    """
    'high'  = the backtest-proven trio (VWAP reclaim + ORB + volume spike) all
              fired — full-size, highest-edge setups.
    'relaxed' = qualified on the looser confluence (e.g. range breakout + bounce)
              but NOT the proven trio — the executor sizes these down.

    In bidirectional mode the trio must fire in the SAME direction as the setup
    (a bearish VWAP rejection + ORB breakdown + distribution spike is an equally
    valid "high" short). In long-only mode direction is ignored, as before.
    """
    proven_triple = (_aligned_score(signals.get("vwap", {}),  direction) > 0
                     and _aligned_score(signals.get("orb", {}),   direction) > 0
                     and _aligned_score(signals.get("spike", {}), direction) > 0)
    return "high" if proven_triple else "relaxed"


def score_hft_setup(signals: dict, direction: str = "bullish") -> int:
    """
    Combine individual signal scores into a composite setup score (0–100).
    Weights: ORB > VWAP reclaim > vol spike > range/bounce > strong bar > momentum.

    Applies a trend-alignment bonus/penalty based on EMA direction:
      +15 pts  if trade aligns with the EMA trend
      -15 pts  if trade is counter-trend (strong fade filter)

    Every directional signal is counted via `_aligned_score`, so in
    bidirectional mode a signal only contributes when it agrees with the setup
    direction. `strong_bar` and `momentum` stay OUT of the confluence floor —
    they are scoring tiebreakers only.
    """
    orb_score    = _aligned_score(signals.get("orb",    {}), direction)
    vwap_score   = _aligned_score(signals.get("vwap",   {}), direction)
    spike_score  = _aligned_score(signals.get("spike",  {}), direction)
    range_score  = _aligned_score(signals.get("range",  {}), direction)
    bounce_score = _aligned_score(signals.get("bounce", {}), direction)

    # strong_bar only adds to the score when the bar's direction aligns with
    # the setup direction — a bearish bar never boosts a bullish setup. (This
    # alignment is enforced in BOTH modes.)
    sb_data          = signals.get("strong_bar", {})
    strong_bar_score = (
        sb_data.get("score", 0)
        if sb_data.get("direction", "none") in (direction, "none")
        else 0
    )

    momentum_score = _aligned_score(signals.get("momentum", {}), direction)
    trend          = signals.get("trend", {})

    # Core directional signals. Momentum and strong_bar are intentionally
    # EXCLUDED from the confluence count: momentum was a noise amplifier in the
    # 60-day 503-ticker backtest; strong_bar is unvalidated at the composite
    # level.  Both act as scoring tiebreakers only.
    core   = [vwap_score, orb_score, spike_score, range_score, bounce_score]
    active = sum(1 for s in core if s > 0)

    # Configurable confluence floor (was a hard 3-of-3 on vwap/orb/spike).
    #   60-day backtest, per-combo per-trade P&L (the original trio study):
    #     vwap+orb+spike          →  +$262/trade  ← the proven, high-conviction combo
    #     vwap+spike (no ORB)     →  -$44/trade
    #     orb+spike  (no VWAP)    →  -$3.54/trade
    #   `range`, `bounce`, and `strong_bar` are NOT in that study — re-run
    #   backtest_hft.py to validate before trusting relaxed-conviction setups.
    if active < HFT_MIN_CONFLUENCE:
        return 0

    # Weighted composite.  The proven trio keeps the heaviest weights;
    # newer signals contribute less until backtested.
    raw = (
        orb_score        * 0.27 +
        vwap_score       * 0.23 +
        spike_score      * 0.22 +
        range_score      * 0.13 +
        bounce_score     * 0.10 +
        strong_bar_score * 0.08 +
        momentum_score   * 0.05
    )

    # Confluence bonus
    if active >= 4:
        raw *= 1.30
    elif active >= 3:
        raw *= 1.20
    elif active >= 2:
        raw *= 1.08

    # Trend alignment modifier
    if trend:
        if direction == "bullish" and trend.get("aligned_bullish"):
            raw += 15   # Trading with the trend
        elif direction == "bullish" and trend.get("aligned_bearish"):
            raw -= 15   # Counter-trend — penalise heavily
        elif direction == "bearish" and trend.get("aligned_bearish"):
            raw += 15
        elif direction == "bearish" and trend.get("aligned_bullish"):
            raw -= 15

    return max(0, min(100, int(raw)))


# ─── Main Scanner ──────────────────────────────────────────────────────────────

def scan_ticker(
    ticker: str,
    interval: str = DEFAULT_INTERVAL,
) -> dict | None:
    """
    Run all intraday signals for a single ticker.
    Returns a setup dict if the composite score >= MIN_SIGNAL_SCORE, else None.

    Gates applied before scoring:
      1. Inverse/leveraged-inverse ETF exclusion
      2. Prime session window (10 AM – 2:30 PM EST)
      3. Trend alignment (EMA20) as score modifier
    """
    # Gate 1: inverse ETF exclusion
    if ticker.upper() in INVERSE_ETF_LIST:
        return None

    df = fetch_intraday(ticker, interval=interval)
    if df.empty or len(df) < 15:
        return None

    # Gate 2: session time — last bar must be inside prime window
    last_bar_time = df.index[-1]
    if not is_prime_session(last_bar_time):
        return None

    vwap = compute_vwap(df)

    # Compute every signal, then resolve direction from them. In long-only mode
    # resolve_direction reduces to "ORB, then range, else bullish" (legacy); in
    # bidirectional mode it is a score-weighted vote across all directional
    # signals, so a cluster of bearish signals drives a short setup.
    signals = {
        "vwap":       detect_vwap_reclaim(df, vwap),
        "orb":        detect_orb_breakout(df),
        "spike":      detect_volume_spike(df),
        "range":      detect_range_breakout(df),
        "bounce":     detect_vwap_bounce(df, vwap),
        "momentum":   detect_momentum_burst(df),
        "strong_bar": detect_strong_bar(df),
        "trend":      detect_trend_alignment(df),   # Gate: EMA filter
    }

    direction = resolve_direction(signals)

    score = score_hft_setup(signals, direction=direction)
    if score < MIN_SIGNAL_SCORE:
        return None

    conviction = hft_conviction(signals, direction)

    last      = df.iloc[-1]
    last_vwap = float(vwap.iloc[-1])

    active_signals = [
        name for name, s in signals.items()
        if name != "trend" and s.get("signal", False)
    ]

    return {
        "ticker":         ticker,
        "setup_score":    score,
        "trade_style":    "day",     # intraday scalp — flat by EOD
        "day_trade_ok":   True,
        "style_reason":   (f"day: {conviction} conviction {direction} intraday momentum "
                           f"({', '.join(active_signals) or 'confluence'})"),
        "conviction":     conviction,
        "direction":      direction,
        "last_price":     round(float(last["Close"]), 2),
        "vwap":           round(last_vwap, 2),
        "pct_vs_vwap":    round((float(last["Close"]) - last_vwap) / last_vwap * 100, 2),
        "trend_aligned":  signals["trend"].get(f"aligned_{direction}", False),
        "pct_vs_ema":     signals["trend"].get("pct_vs_ema", 0),
        "interval":       interval,
        "active_signals": active_signals,
        "signal_details": {
            name: s.get("detail", "")
            for name, s in signals.items()
            if name != "trend" and s.get("signal", False)
        },
        "timestamp":      df.index[-1].isoformat(),
    }


def run_hft_scan(
    csv_path: str | None = "sp500.csv",
    universe_limit: int = UNIVERSE_LIMIT,
    interval: str = DEFAULT_INTERVAL,
    min_score: int = MIN_SIGNAL_SCORE,
    rotation_key: str | None = None,
) -> list[dict]:
    """
    Full intraday scan pipeline.

    Returns a list of setup dicts sorted by score descending.
    """
    print("\n" + "=" * 60)
    print("  HFT INTRADAY SCANNER  —  Strategy 3")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  interval={interval}")
    print("=" * 60 + "\n")

    symbols = load_universe(csv_path=csv_path, limit=universe_limit, rotation_key=rotation_key)
    liquid  = filter_universe(
        symbols=symbols,
        min_price=MIN_PRICE,
        min_avg_volume=MIN_AVG_VOLUME,
        min_avg_dollar_volume=MIN_DOLLAR_VOLUME,
    )
    print(f"[HFT Scanner] {len(liquid)} liquid symbols to scan\n")

    results = []
    for ticker in liquid:
        setup = scan_ticker(ticker, interval=interval)
        if setup and setup["setup_score"] >= min_score:
            results.append(setup)
            sigs = ", ".join(setup["active_signals"]) or "none"
            print(
                f"[HFT Scanner] ✅ {ticker} | Score: {setup['setup_score']}/100 | "
                f"Dir: {setup['direction']} | Signals: {sigs}"
            )

    results.sort(key=lambda x: x["setup_score"], reverse=True)

    print("\n" + "=" * 60)
    print(f"  HFT SETUPS FOUND: {len(results)}")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        print(
            f"\n#{i} {r['ticker']} | Score: {r['setup_score']}/100 | {r['direction'].upper()}"
            f"\n    Price: ${r['last_price']} | VWAP: ${r['vwap']} | "
            f"vs VWAP: {r['pct_vs_vwap']:+.2f}%"
            f"\n    Signals: {', '.join(r['active_signals'])}"
        )
        for sig, detail in r["signal_details"].items():
            print(f"    [{sig}] {detail}")

    return results


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HFT Intraday Scanner — Strategy 3")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL,
                        choices=["1m", "2m", "5m"],
                        help="Bar interval (default 5m)")
    parser.add_argument("--limit",    type=int, default=UNIVERSE_LIMIT,
                        help=f"Universe size (default {UNIVERSE_LIMIT})")
    parser.add_argument("--min-score", type=int, default=MIN_SIGNAL_SCORE,
                        help=f"Minimum score threshold (default {MIN_SIGNAL_SCORE})")
    args = parser.parse_args()

    run_hft_scan(
        universe_limit=args.limit,
        interval=args.interval,
        min_score=args.min_score,
    )
