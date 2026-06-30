"""
pead_scanner.py  —  Strategy 4: Post-Earnings / News Gap Drift Scanner

Detects the well-documented Post-Earnings Announcement Drift (PEAD) anomaly and
generalises it to any large news-driven gap: after an outsized price move on
heavy volume, the stock tends to keep drifting in the move's direction for days
to weeks. We trade that continuation with directional options.

Why generalise beyond scheduled earnings?
    Free data sources only reliably give the NEXT (future) earnings date, not the
    last one. But the earnings *reaction itself* is what we trade — an abnormal
    daily move on heavy volume IS the signal, whether the catalyst was an
    earnings print, guidance, an FDA decision, an analyst action, or an M&A
    headline. "News is always coming out", so a price-anomaly trigger captures
    more of the opportunity than an earnings-calendar gate alone.

Signal (all computed from DAILY bars):
    1. Event move   — |return on the event day| is a large multiple of the
                      stock's own recent daily volatility (a z-score), AND above
                      an absolute floor so we skip noise in low-vol names.
    2. Volume surge — event-day volume >> its recent average (confirms it was a
                      real, news-driven move, not thin drift).
    3. Recency      — the event happened within the last few trading days (we
                      enter into FRESH drift, not a stale one).
    4. Drift hold   — since the event, price has retained (ideally extended) the
                      move rather than fully retracing it.
    5. Trend align  — a small bonus when the drift direction agrees with the
                      50-day trend.

Direction: up-move event -> bullish (calls); down-move event -> bearish (puts).

Conviction tiering (mirrors the HFT strategy's high/relaxed sizing):
    'high'    — a very large, high-volume gap (almost certainly an earnings or
                major-news event): full size.
    'relaxed' — a moderate qualifying gap: sized DOWN by the executor.

The detector functions are PURE (operate on a passed-in DataFrame) so the
backtester can replay the exact live logic over historical data.
"""

import argparse
import datetime
import time

import numpy as np
import pandas as pd

from mawitek.infra.logger import get_logger
# Regime helpers live in market_regime (single source of truth, one SPY fetch/day).
# Re-imported here so existing call sites (dashboard_state, backtest_pead, tests)
# that reference pead_scanner.is_bear_regime / REGIME_* keep working.
from mawitek.data.market_regime import current_regime, is_bear_regime, REGIME_SMA_DAYS, REGIME_TICKER

log = get_logger("pead_scanner")


# ─── Config ──────────────────────────────────────────────────────────────────

EVENT_MIN_Z          = 2.5     # event move >= this many σ of recent daily returns
EVENT_MIN_GAP_PCT    = 0.04    # ...and at least a 4% absolute move (noise floor)
EVENT_MIN_VOL_MULT   = 1.8     # event-day volume >= 1.8x its recent average
EVENT_LOOKBACK_DAYS  = 3       # event must be within the last N trading days
MIN_HOLD_FRAC        = 0.5     # since the event, >= 50% of the move retained
VOL_BASELINE_DAYS    = 20      # window for the volatility / volume baselines
TREND_SMA_DAYS       = 50      # trend filter window

MIN_SETUP_SCORE      = 55      # composite score floor to qualify

# Only trade drift that AGREES with the 50-day trend. This is the single
# biggest edge in the backtest: it turns the raw signal (PF ~0.97, slightly
# negative) into PF ~1.2 by skipping counter-trend gaps that tend to mean-revert
# rather than drift. Symmetric and non-overfit — keep this on.
REQUIRE_TREND_ALIGNMENT = True

# Bearish (down-gap → puts) drift, gated by the broad-market regime (SPY vs its
# 200-day SMA). Built so puts would only fire when BOTH the market and the stock
# are in a downtrend — i.e. exactly when shorting "should" work.
#
#   VALIDATION (4-yr backtest incl. the 2022 bear, 232 bear-regime days):
#   it FAILED. The regime-gated short side won only 8% of trades, PF 0.13,
#   -$12.7k — and dragged the strategy from +$16.4k (long-only) down to +$3.6k.
#   Reason is structural, not a model bug: 2022-style bears are full of violent
#   snapback rallies, and LONG PUTS are expensive (high IV) and bleed theta/IV
#   crush, so they're the wrong instrument for bearish drift. Trend+regime
#   double-gating did not save it.
#
#   => DEFAULT FALSE: the strategy stays pure LONG-ONLY. The machinery is kept
#   (point-in-time, faithful backtest) for a future short-side attempt that uses
#   a credit structure (e.g. bear call spread) instead of long puts — re-validate
#   before ever enabling.
BEARISH_REGIME_FILTER = False   # master switch; True = regime-gated puts (validated-negative)
# REGIME_TICKER / REGIME_SMA_DAYS are imported from market_regime (above).

# Conviction: a gap this large + this heavy is almost certainly earnings / major
# news -> full size. Anything qualifying but smaller is "relaxed" -> half size.
HIGH_CONVICTION_Z        = 4.0
HIGH_CONVICTION_GAP_PCT  = 0.07
HIGH_CONVICTION_VOL_MULT = 3.0

# Inverse / leveraged ETFs: drift logic is structurally wrong on these.
INVERSE_ETF_LIST = {
    "SQQQ", "SDOW", "SPXS", "SPXU", "TECS", "FAZ", "LABD",
    "SOXS", "UVXY", "SVXY", "VIXY", "SDS", "PSQ", "DOG",
}


# ─── Indicators ──────────────────────────────────────────────────────────────

def _daily_return_vol(close: pd.Series, end_idx: int, window: int = VOL_BASELINE_DAYS) -> float:
    """Std of daily returns over the `window` bars BEFORE end_idx (the event day)."""
    rets = close.pct_change(fill_method=None)
    sample = rets.iloc[max(0, end_idx - window):end_idx]
    if len(sample) < 5:
        return float("nan")
    return float(sample.std())


def _avg_volume(volume: pd.Series, end_idx: int, window: int = VOL_BASELINE_DAYS) -> float:
    sample = volume.iloc[max(0, end_idx - window):end_idx]
    if len(sample) < 5:
        return float("nan")
    return float(sample.mean())


def _trend_direction(close: pd.Series, end_idx: int, window: int = TREND_SMA_DAYS) -> str:
    """'bullish'/'bearish'/'none' from the slope of the SMA up to end_idx."""
    if end_idx < window + 2:
        return "none"
    sma = close.rolling(window).mean()
    now = float(sma.iloc[end_idx])
    prior = float(sma.iloc[end_idx - 3])
    if np.isnan(now) or np.isnan(prior):
        return "none"
    if now > prior:
        return "bullish"
    if now < prior:
        return "bearish"
    return "none"


# ─── Market Regime (gates the bearish/put side) ──────────────────────────────
# is_bear_regime() (pure) and the cached current_regime() live in market_regime.py
# — the single source of truth with one SPY fetch/day — and are imported at the
# top of this file.

def bearish_allowed_now() -> bool:
    """Live: is the put side currently enabled? (master switch AND bear regime)."""
    return BEARISH_REGIME_FILTER and current_regime()["state"] == "bear"


# ─── Event Detection ─────────────────────────────────────────────────────────

def _event_at(df: pd.DataFrame, i: int) -> dict | None:
    """
    Test whether day `i` is a qualifying gap/news event.

    Returns a dict describing the event, or None if day i doesn't qualify.
    """
    if i < VOL_BASELINE_DAYS + 1 or i >= len(df):
        return None

    close = df["Close"]
    volume = df["Volume"]

    prev_close = float(close.iloc[i - 1])
    event_close = float(close.iloc[i])
    if prev_close <= 0:
        return None

    move = (event_close - prev_close) / prev_close
    base_vol = _daily_return_vol(close, i)
    avg_vol = _avg_volume(volume, i)
    if not np.isfinite(base_vol) or base_vol <= 0 or not np.isfinite(avg_vol) or avg_vol <= 0:
        return None

    move_z = move / base_vol
    vol_mult = float(volume.iloc[i]) / avg_vol

    if (abs(move_z) < EVENT_MIN_Z
            or abs(move) < EVENT_MIN_GAP_PCT
            or vol_mult < EVENT_MIN_VOL_MULT):
        return None

    return {
        "event_idx":   i,
        "direction":   "bullish" if move > 0 else "bearish",
        "move":        move,
        "move_z":      move_z,
        "vol_mult":    vol_mult,
        "prev_close":  prev_close,
        "event_close": event_close,
    }


def conviction_for(event: dict) -> str:
    """'high' for a clear earnings/major-news-scale gap, else 'relaxed'."""
    if (abs(event["move_z"]) >= HIGH_CONVICTION_Z
            and abs(event["move"]) >= HIGH_CONVICTION_GAP_PCT
            and event["vol_mult"] >= HIGH_CONVICTION_VOL_MULT):
        return "high"
    return "relaxed"


def score_drift_setup(event: dict, held_frac: float, days_since: int,
                      trend: str) -> int:
    """
    Composite 0–100 score for a drift setup.

    Bigger, higher-volume surprises drift more (the core PEAD finding), fresh
    events are better than stale ones, and continuation that holds/extends the
    move is worth more than a fade.
    """
    direction = event["direction"]

    # Surprise magnitude (z-score) — the strongest predictor. (up to 40)
    z = abs(event["move_z"])
    mag = min(40, 12 + (z - EVENT_MIN_Z) * 10)

    # Volume surge (up to 20)
    vol = min(20, (event["vol_mult"] - EVENT_MIN_VOL_MULT) * 8 + 6)

    # Drift quality: held_frac 0.5→1.0 is holding, >1.0 is extending. (up to 25)
    drift = min(25, max(0, (held_frac - MIN_HOLD_FRAC) / 0.5 * 18) + 7)

    # Recency: 1 day ago best, decays out to the lookback. (up to 10)
    recency = max(0, 10 - (days_since - 1) * 4)

    raw = mag + vol + drift + recency

    # Trend alignment modifier (±10)
    if trend == direction:
        raw += 10
    elif trend in ("bullish", "bearish"):
        raw -= 10

    return int(max(0, min(100, raw)))


def detect_drift(df: pd.DataFrame, as_of: int | None = None,
                 bearish_allowed: bool = False) -> dict | None:
    """
    Look for a qualifying, still-fresh drift setup as of bar `as_of`
    (default: the last bar). PURE — operates only on the passed DataFrame.

    Scans the last EVENT_LOOKBACK_DAYS bars (most recent first) for an event,
    confirms the move has held, scores it, and returns a setup dict or None.

    `bearish_allowed` — whether short (put) drift may trade. The caller decides
    this from the market regime (see bearish_allowed_now / is_bear_regime) so
    this function stays pure and point-in-time. When False (default), only
    bullish drift qualifies (long-only).
    """
    if df is None or df.empty:
        return None
    n = len(df)
    t = (n - 1) if as_of is None else as_of
    if t < VOL_BASELINE_DAYS + 2:
        return None

    close = df["Close"]
    as_of_close = float(close.iloc[t])
    trend = _trend_direction(close, t)   # depends only on `t` — compute once

    # Most recent event first so we enter into the freshest drift.
    for days_since in range(1, EVENT_LOOKBACK_DAYS + 1):
        e_idx = t - days_since
        if e_idx < VOL_BASELINE_DAYS + 1:
            break
        event = _event_at(df, e_idx)
        if not event:
            continue

        # How much of the event move has been retained since (1.0 = fully held,
        # >1.0 = drifted further, <0 = fully reversed). Same sign convention as
        # the move so it works for both up and down events.
        move_abs = event["event_close"] - event["prev_close"]
        if move_abs == 0:
            continue
        held_frac = (as_of_close - event["prev_close"]) / move_abs
        if held_frac < MIN_HOLD_FRAC:
            continue   # the move faded — no tradable drift

        # Direction gate: bearish only when the regime allows it (else long-only).
        if event["direction"] == "bearish" and not bearish_allowed:
            continue

        # Trend gate: only ride drift that agrees with the prevailing trend.
        if REQUIRE_TREND_ALIGNMENT and trend != event["direction"]:
            continue

        score = score_drift_setup(event, held_frac, days_since, trend)
        if score < MIN_SETUP_SCORE:
            continue

        return {
            "direction":   event["direction"],
            "setup_score": score,
            "conviction":  conviction_for(event),
            "event_move":  round(event["move"] * 100, 2),
            "move_z":      round(event["move_z"], 2),
            "vol_mult":    round(event["vol_mult"], 2),
            "days_since":  days_since,
            "held_frac":   round(held_frac, 2),
            "trend":       trend,
            "trend_aligned": trend == event["direction"],
            "event_close": round(event["event_close"], 2),
            "prev_close":  round(event["prev_close"], 2),
            "as_of_close": round(as_of_close, 2),
        }

    return None


# ─── Daily-bar cache (live scan only) ────────────────────────────────────────
# The live loop rescans the whole universe every ~30 min, but daily bars barely
# change within a session for a multi-day signal. Reuse a per-ticker fetch for an
# hour so the loop doesn't re-pull dozens of daily histories every cycle. The
# backtest calls yfinance directly and is unaffected.
DAILY_LOOKBACK_DAYS = 120       # calendar days (~84 trading) — headroom for SMA50
DAILY_CACHE_TTL_SEC = 3600
_daily_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def clear_daily_cache() -> None:
    _daily_cache.clear()


def _fetch_daily_cached(ticker: str, days: int) -> pd.DataFrame:
    cached = _daily_cache.get(ticker)
    if cached is not None and (time.time() - cached[0]) < DAILY_CACHE_TTL_SEC:
        return cached[1]
    from mawitek.data.market_data import get_daily_bars   # local import keeps detectors import-light
    df = get_daily_bars(ticker, days=days)
    if not df.empty:
        _daily_cache[ticker] = (time.time(), df)
    return df


# ─── Live Scan ───────────────────────────────────────────────────────────────

def scan_ticker(ticker: str, daily_lookback: int = DAILY_LOOKBACK_DAYS,
                bearish_allowed: bool = False) -> dict | None:
    """Fetch recent daily bars (Tradier, cached) and run the drift detector live."""
    if ticker.upper() in INVERSE_ETF_LIST:
        return None

    df = _fetch_daily_cached(ticker, daily_lookback)
    # Need enough history for the 50-day TREND gate (not just the vol baseline),
    # otherwise _trend_direction returns "none" and REQUIRE_TREND_ALIGNMENT would
    # silently reject every setup.
    if df.empty or len(df) < TREND_SMA_DAYS + 5:
        return None

    setup = detect_drift(df, bearish_allowed=bearish_allowed)
    if not setup:
        return None
    setup["ticker"] = ticker
    setup["timestamp"] = df.index[-1].isoformat()
    # Style tags for the shared scanner-setups list / subscriber alerts.
    setup["trade_style"] = "swing"      # multi-day drift hold
    setup["day_trade_ok"] = False
    setup["style_reason"] = (f"swing: {setup['direction']} news/earnings drift — "
                             f"{setup['event_move']:+.1f}% gap ({setup['move_z']:+.1f}σ) "
                             f"{setup['days_since']}d ago, held {setup['held_frac']:.0%}, "
                             f"{setup['conviction']} conviction")
    return setup


def run_pead_scan(csv_path: str | None = "sp500.csv",
                  universe_limit: int = 120,
                  min_score: int = MIN_SETUP_SCORE,
                  rotation_key: str | None = None) -> list[dict]:
    """Full live scan pipeline. Returns setups sorted by score (desc)."""
    from mawitek.data.universe import load_universe
    from mawitek.data.market_filter import filter_universe

    print("\n" + "=" * 60)
    print("  PEAD / NEWS-DRIFT SCANNER  —  Strategy 4")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60 + "\n")

    symbols = load_universe(csv_path=csv_path, limit=universe_limit, rotation_key=rotation_key)
    liquid = filter_universe(symbols, min_price=5.0,
                             min_avg_volume=1_000_000,
                             min_avg_dollar_volume=20_000_000)

    # Regime decides whether the put side is live this scan (computed once).
    bearish_ok = bearish_allowed_now()
    regime = "BEAR → puts enabled" if bearish_ok else "BULL/neutral → long-only"
    print(f"[PEAD Scanner] {len(liquid)} liquid symbols to scan | Market regime: {regime}\n")

    results = []
    for ticker in liquid:
        setup = scan_ticker(ticker, bearish_allowed=bearish_ok)
        if setup and setup["setup_score"] >= min_score:
            results.append(setup)
            print(f"[PEAD Scanner] ✅ {ticker} | {setup['direction'].upper()} | "
                  f"Score {setup['setup_score']} | move {setup['event_move']:+.1f}% "
                  f"({setup['move_z']:+.1f}σ) {setup['days_since']}d ago | "
                  f"held {setup['held_frac']:.0%} | {setup['conviction']}")

    results.sort(key=lambda s: s["setup_score"], reverse=True)
    print(f"\n[PEAD Scanner] {len(results)} drift setups found\n")
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PEAD / news-drift scanner — Strategy 4")
    parser.add_argument("--limit", type=int, default=120, help="Universe size")
    parser.add_argument("--min-score", type=int, default=MIN_SETUP_SCORE)
    args = parser.parse_args()
    run_pead_scan(universe_limit=args.limit, min_score=args.min_score)
