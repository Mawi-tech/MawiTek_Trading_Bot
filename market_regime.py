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
"""

from __future__ import annotations

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


def clear_cache() -> None:
    """Drop the cached regime (new session / tests)."""
    _cache.update({"day": None, "regime": None})
