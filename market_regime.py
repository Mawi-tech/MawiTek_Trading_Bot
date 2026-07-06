"""
market_regime.py — one shared read of the broad-market regime (SPY vs 200-day SMA).

Several parts of the bot need to know whether the market is in a bull or bear
regime: the bear-market risk throttle (risk_manager), the PEAD bearish gate
(pead_scanner), and the dashboard. Rather than each computing it and each pulling
SPY's 200-day history separately, this module is the single source of truth and
performs at most ONE network fetch per ET trading day (cached), shared across the
whole process.

    is_bear_regime(spy_close)  PURE — True if the last close is below the 200d SMA.
                               Used point-in-time by live code AND by backtests
                               (which pass a historical close series).
    current_regime()           Cached-per-day live read: state + SPY/SMA levels.
    is_bear_market()           Convenience bool over current_regime().

It also owns the INTRADAY red-day read — "is the market red RIGHT NOW?" — which
the daily regime cannot see (a −2% SPY day inside a bull regime looks like "bull"
all day to current_regime()). Same pure/live split:

    classify_red_day(chg, prev) PURE — "ok"/"weak"/"red" from SPY's intraday %
                                change, with hysteresis so a tripped state only
                                clears once SPY genuinely recovers.
    intraday_market_status()    TTL-cached live read (one SPY quote per
                                RED_DAY_TTL_SEC per process). Fails OPEN to
                                "unknown" like current_regime().
    is_red_day()                Convenience bool — market is red right now.
    is_market_weak()            Bear regime OR intraday weak/red — the signal
                                iv_rank uses to pick its credit-spread direction.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from logger import get_logger
from utils import today_est

log = get_logger("market_regime")

REGIME_TICKER   = "SPY"
REGIME_SMA_DAYS = 200


def is_bear_regime(spy_close: pd.Series) -> bool:
    """True when SPY's latest close is below its 200-day SMA. PURE (no I/O)."""
    if spy_close is None or len(spy_close) < REGIME_SMA_DAYS:
        return False   # not enough history -> assume bull (the safe, long-only default)
    sma  = float(spy_close.rolling(REGIME_SMA_DAYS).mean().iloc[-1])
    last = float(spy_close.iloc[-1])
    return bool(np.isfinite(sma)) and last < sma


# The regime is a once-a-day figure, so we fetch SPY at most once per ET trading
# day and serve every caller from this cache — no repeated downloads, so reading
# the regime in a hot path (e.g. pre_trade_check) costs nothing after the first.
_cache: dict = {"day": None, "regime": None}


def current_regime() -> dict:
    """
    Current market regime, cached per ET trading day.

    Returns {"state": "bull"|"bear"|"unknown", "spy": float|None,
             "sma": float|None, "detail": str}. Fails safe to "unknown" (never
    raises) so callers default to their own conservative behavior if SPY data is
    briefly unavailable.
    """
    day = today_est().isoformat()
    if _cache["day"] == day and _cache["regime"] is not None:
        return _cache["regime"]

    regime = {"state": "unknown", "spy": None, "sma": None, "detail": "SPY data unavailable"}
    try:
        from market_data import get_daily_bars
        # Need >= REGIME_SMA_DAYS *trading* rows; ~0.69 trading days per calendar
        # day, so fetch enough calendar days to clear the 200-row SMA with margin.
        df = get_daily_bars(REGIME_TICKER, days=REGIME_SMA_DAYS + 160)
        if not df.empty and len(df) >= REGIME_SMA_DAYS:
            close = df["Close"]
            sma   = float(close.rolling(REGIME_SMA_DAYS).mean().iloc[-1])
            last  = float(close.iloc[-1])
            bear  = last < sma
            regime = {
                "state":  "bear" if bear else "bull",
                "spy":    round(last, 2),
                "sma":    round(sma, 2),
                "detail": f"SPY ${last:,.0f} {'below' if bear else 'above'} 200d SMA ${sma:,.0f}",
            }
    except Exception as e:
        log.warning("regime check failed: %s", e)

    _cache.update({"day": day, "regime": regime})
    return regime


def is_bear_market() -> bool:
    """Convenience: True when the cached current regime is bearish."""
    return current_regime()["state"] == "bear"


# ─── Intraday red-day detection ──────────────────────────────────────────────────
# The 200d-SMA regime is a once-a-day figure, so the long-biased strategies keep
# buying at full size all through a sharp red session in a bull market. These
# thresholds classify SPY's live intraday % change:
#   > RED_DAY_WEAK_PCT            "ok"   — normal tape
#   <= RED_DAY_WEAK_PCT (~1σ)     "weak" — risk gate halves new long budgets
#   <= RED_DAY_RED_PCT  (~2σ)     "red"  — risk gate pauses new long entries
# Hysteresis: once tripped, the day stays at least "weak" until SPY recovers
# above RED_DAY_RECOVER_PCT, so the gate doesn't flap around the threshold.

RED_DAY_TICKER      = REGIME_TICKER
RED_DAY_WEAK_PCT    = -0.75   # ≈1σ of a normal SPY day -> throttle
RED_DAY_RED_PCT     = -1.50   # ≈2σ, ~5% of sessions (the bleed days) -> pause
RED_DAY_RECOVER_PCT = -0.40   # tripped state clears only above this
RED_DAY_TTL_SEC     = 600     # at most one SPY quote per 10 min per process


def classify_red_day(chg_pct: float | None, prev_state: str = "ok") -> str:
    """
    Classify the intraday market state from SPY's % change. PURE (no I/O).

    Returns "ok" | "weak" | "red" | "unknown". `prev_state` carries the
    hysteresis: if the day already tripped ("weak"/"red") and SPY has not yet
    recovered above RED_DAY_RECOVER_PCT, the state stays at least "weak" —
    though a "red" pause relaxes to "weak" once the drop eases past the red line.
    """
    if chg_pct is None:
        return "unknown"
    try:
        chg = float(chg_pct)
    except (TypeError, ValueError):
        return "unknown"
    if not np.isfinite(chg):
        return "unknown"
    if chg <= RED_DAY_RED_PCT:
        return "red"
    if chg <= RED_DAY_WEAK_PCT:
        return "weak"
    if prev_state in ("weak", "red") and chg <= RED_DAY_RECOVER_PCT:
        return "weak"
    return "ok"


# TTL cache: {"day", "ts", "status", "prev_state"}. `prev_state` remembers the
# last KNOWN classification for the hysteresis — a transient data gap ("unknown")
# must not silently clear a tripped day.
_intraday_cache: dict = {"day": None, "ts": 0.0, "status": None, "prev_state": "ok"}


def intraday_market_status() -> dict:
    """
    Live intraday market state, cached for RED_DAY_TTL_SEC.

    Returns {"state": "ok"|"weak"|"red"|"unknown", "spy_chg_pct": float|None,
             "detail": str}. Fails OPEN (never raises): MOCK_MODE, a missing
    quote, or any error yields "unknown" so callers keep their normal behavior.
    """
    day = today_est().isoformat()
    now = time.time()
    if _intraday_cache["day"] != day:
        # New session: hysteresis resets — yesterday's trip doesn't carry over.
        _intraday_cache.update({"day": day, "ts": 0.0, "status": None, "prev_state": "ok"})
    elif _intraday_cache["status"] is not None and now - _intraday_cache["ts"] < RED_DAY_TTL_SEC:
        return _intraday_cache["status"]

    status = {"state": "unknown", "spy_chg_pct": None, "detail": "SPY quote unavailable"}
    try:
        from tradier_client import get_quote_details
        quote = get_quote_details([RED_DAY_TICKER]).get(RED_DAY_TICKER) or {}
        state = classify_red_day(quote.get("change_pct"), _intraday_cache["prev_state"])
        if state != "unknown":
            chg = float(quote["change_pct"])
            status = {
                "state":       state,
                "spy_chg_pct": round(chg, 2),
                "detail":      f"SPY {chg:+.2f}% intraday — {state}",
            }
            _intraday_cache["prev_state"] = state
    except Exception as e:
        log.warning("intraday red-day check failed: %s", e)

    _intraday_cache.update({"ts": now, "status": status})
    return status


def is_red_day() -> bool:
    """Convenience: True when SPY is deeply red right now (state == "red")."""
    return intraday_market_status()["state"] == "red"


def is_market_weak() -> bool:
    """
    True when the market argues against NEW bullish exposure: daily bear regime
    OR the intraday tape is weak/red. This is the direction signal for the
    iv_rank credit-spread fallback (bull-put vs bear-call).
    """
    if is_bear_market():
        return True
    return intraday_market_status()["state"] in ("weak", "red")


def clear_cache() -> None:
    """Drop the cached regime and intraday status (new session / tests)."""
    _cache.update({"day": None, "regime": None})
    _intraday_cache.update({"day": None, "ts": 0.0, "status": None, "prev_state": "ok"})
