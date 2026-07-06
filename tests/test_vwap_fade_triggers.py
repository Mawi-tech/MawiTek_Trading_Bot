"""Tests for the VWAP mean-reversion fade detector, its pure helpers, the
trend-day skip gate, the ships-dark guard, and strategy registration."""

import numpy as np
import pandas as pd

import vwap_fade_scanner as vf
import risk_manager as rm
import user_config as uc


# ── frame builders ───────────────────────────────────────────────────────────

def _frame(pre: np.ndarray, move: np.ndarray, stall_up: bool) -> pd.DataFrame:
    """Session frame: a `pre` context then a sharp `move`, with a final stall bar.
    stall_up=True → a bullish stall (close off the low / higher low) for a CALL
    fade; False → a bearish stall for a PUT fade."""
    close = np.concatenate([pre, move])
    idx = pd.date_range("2026-07-06 10:00", periods=len(close), freq="5min")
    o = close.copy(); h = close + 0.08; l = close - 0.08
    if stall_up:
        l[-1] = close[-2] - 0.02; h[-1] = close[-1] + 0.15
    else:
        h[-1] = close[-2] + 0.02; l[-1] = close[-1] - 0.15
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": close, "Volume": 1e6}, index=idx)


_FLAT = np.full(30, 100.0) + np.array([0.02 * (-1) ** i for i in range(30)])
_FLUSH = np.array([99.3, 98.6, 97.9, 97.5, 97.6])     # sharp down then stall
_RIP   = np.array([100.7, 101.4, 102.1, 102.5, 102.4]) # sharp up then stall


def _detect(df):
    return vf.detect_vwap_fade(df, vf.compute_vwap(df))


# ── detector ─────────────────────────────────────────────────────────────────

def test_call_fade_fires_on_flush_and_stall():
    sig = _detect(_frame(_FLAT, _FLUSH, stall_up=True))
    assert sig["signal"] and sig["direction"] == "bullish"   # buy a CALL
    assert sig["score"] > 0


def test_put_fade_fires_on_rip_and_stall():
    sig = _detect(_frame(_FLAT, _RIP, stall_up=False))
    assert sig["signal"] and sig["direction"] == "bearish"   # buy a PUT


def test_no_signal_when_not_stretched():
    # Tiny move — never reaches the z / %-of-VWAP stretch floor.
    sig = _detect(_frame(_FLAT, np.array([100.0, 100.05, 99.98, 100.02, 100.0]), stall_up=True))
    assert not sig["signal"]


def test_no_signal_without_reversal_bar():
    # Stretched + extreme, but the last bar is still ACCELERATING down (no stall):
    # closes on its low, a lower low than the prior bar → a trend leg, not a fade.
    df = _frame(_FLAT, np.array([99.3, 98.6, 97.9, 97.2, 96.4]), stall_up=True)
    c = float(df.iloc[-1]["Close"])
    df.iloc[-1, df.columns.get_loc("High")] = c + 0.20    # close sits at the LOW
    df.iloc[-1, df.columns.get_loc("Low")]  = c - 0.02
    df.iloc[-1, df.columns.get_loc("Open")] = float(df.iloc[-2]["Close"])  # down bar
    assert not vf.is_reversal_bar(df, "call")             # precondition: no stall
    assert not vf.detect_vwap_fade(df, vf.compute_vwap(df))["signal"]


def test_trend_day_is_skipped():
    # A clean one-way downtrend that stays stretched: the killer gate must skip it
    # even though z and RSI look extreme.
    dtrend = 100 - np.linspace(0, 5, 35)
    sig = _detect(_frame(dtrend[:30], dtrend[30:], stall_up=True))
    assert not sig["signal"]
    assert "trend day" in sig["detail"]


def test_conviction_is_valid_and_scores_high():
    # A deep, extreme flush scores near the top and yields a valid conviction tier.
    sig = _detect(_frame(_FLAT, _FLUSH, stall_up=True))
    assert sig["conviction"] in ("high", "relaxed")
    assert sig["score"] >= 80          # deep stretch + RSI extreme → high score


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_efficiency_ratio_ramp_vs_chop():
    assert vf.efficiency_ratio(pd.Series(np.arange(21.0)), 20) == 1.0     # clean trend
    zig = pd.Series([0, 1] * 11, dtype=float)
    assert vf.efficiency_ratio(zig, 20) < 0.1                             # pure chop


def test_vwap_stretch_z_sign_and_zero_band():
    df = _frame(_FLAT, _FLUSH, stall_up=True)
    assert vf.vwap_stretch_z(df, vf.compute_vwap(df)) < 0                 # below VWAP
    # A perfectly flat frame has ~zero band → z falls back to 0 (no divide blowup).
    flat = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0, "Volume": 1e6},
                        index=pd.date_range("2026-07-06 10:00", periods=20, freq="5min"))
    assert vf.vwap_stretch_z(flat, vf.compute_vwap(flat)) == 0.0


def test_is_reversal_bar_both_sides():
    up = _frame(_FLAT, _FLUSH, stall_up=True)
    assert vf.is_reversal_bar(up, "call")       # bullish stall after a flush
    down = _frame(_FLAT, _RIP, stall_up=False)
    assert vf.is_reversal_bar(down, "put")       # bearish stall after a rip


def test_is_trend_day_direction_aware():
    # A steady UPtrend opposes a PUT (bearish) fade → skip; a CALL (bullish) fade
    # is with the trend so this particular gate does not trip on it.
    up = 100 + np.linspace(0, 5, 35)
    df = _frame(up[:30], up[30:], stall_up=False)
    vwap = vf.compute_vwap(df)
    assert vf.is_trend_day(df, vwap, "bearish") is True


# ── ships-dark guard ─────────────────────────────────────────────────────────

def test_scan_returns_empty_while_disabled():
    assert vf.ENABLE_VWAP_FADE is False           # default: dark
    assert vf.run_vwap_fade_scan() == []


def test_scan_runs_when_enabled(monkeypatch):
    # Flip the flag; with no universe data (MOCK_MODE) it still returns a list,
    # proving the guard is the only thing gating it off.
    monkeypatch.setattr(vf, "ENABLE_VWAP_FADE", True)
    monkeypatch.setattr(vf, "load_universe", lambda **k: [])
    monkeypatch.setattr(vf, "filter_universe", lambda **k: [])
    assert vf.run_vwap_fade_scan() == []


# ── registration / classification ────────────────────────────────────────────

def test_registered_as_day_strategy():
    assert rm.classify_trade_type("vwap_fade") == "day"
    assert "vwap_fade" in rm.DAY_STRATEGIES
    assert "vwap_fade" not in rm.SWING_STRATEGIES
    assert "vwap_fade" in rm.LONG_VEGA_STRATEGIES
    assert "vwap_fade" in rm.STRATEGY_ALLOCATION_PCT
    assert "vwap_fade" in uc.KNOWN_STRATEGIES
    assert "vwap_fade" in uc._DAY_STRATEGIES


def test_paused_until_validated():
    # Ships dark: paused at runtime AND behind the scanner flag.
    assert "vwap_fade" in rm.PAUSED_STRATEGIES
    assert vf.ENABLE_VWAP_FADE is False


def test_not_red_day_gated():
    # A mean-reversion fade must NOT be paused on red days — its call side is the
    # oversold-flush bet. Freeze that decision so a future edit can't silently gate it.
    assert "vwap_fade" not in rm.RED_DAY_GATED_STRATEGIES
