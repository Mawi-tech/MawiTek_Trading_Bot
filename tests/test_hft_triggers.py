"""Tests for the new intraday triggers + confluence/conviction logic."""

import pandas as pd
import mawitek.strategies.hft_scanner as hs


def _df(highs, lows, closes, vols, opens=None):
    opens = opens or closes
    idx = pd.date_range("2026-06-01 14:00", periods=len(closes), freq="5min", tz="UTC")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


# ── Range breakout ───────────────────────────────────────────────────────────

def test_range_breakout_fires_on_new_high_with_volume():
    # 14 flat bars then a breakout bar above the range on big volume
    n = 14
    highs = [100] * n + [103]
    lows  = [99]  * n + [101]
    closes = [99.5] * n + [102.5]
    vols  = [1000] * n + [3000]
    r = hs.detect_range_breakout(_df(highs, lows, closes, vols), lookback=12)
    assert r["signal"] and r["direction"] == "bullish"
    assert r["score"] > 0


def test_range_breakout_silent_inside_range():
    n = 14
    df = _df([100]*n, [99]*n, [99.5]*n, [1000]*n)
    r = hs.detect_range_breakout(df, lookback=12)
    assert not r["signal"]


def test_range_breakdown_bearish():
    n = 14
    highs = [100]*n + [99]
    lows  = [99]*n + [96]
    closes = [99.5]*n + [96.5]
    vols = [1000]*n + [3000]
    r = hs.detect_range_breakout(_df(highs, lows, closes, vols), lookback=12)
    assert r["signal"] and r["direction"] == "bearish"


# ── VWAP bounce ──────────────────────────────────────────────────────────────

def test_vwap_bounce_fires_on_pullback_and_turn():
    # Price above VWAP, dips to test it, turns back up
    closes = [101, 101.5, 102, 101, 100.6, 101.2]
    highs  = [c + 0.3 for c in closes]
    lows   = [c - 0.3 for c in closes]
    vols   = [1000, 1000, 1000, 1200, 1500, 1800]
    df = _df(highs, lows, closes, vols)
    vwap = pd.Series([100.5] * len(closes), index=df.index)  # flat VWAP below price
    r = hs.detect_vwap_bounce(df, vwap)
    assert r["signal"]


def test_vwap_bounce_silent_when_below_vwap():
    closes = [99, 98.5, 98, 98.2, 98.1, 98.3]
    df = _df([c+0.2 for c in closes], [c-0.2 for c in closes], closes, [1000]*6)
    vwap = pd.Series([100] * 6, index=df.index)  # price entirely below VWAP
    assert not hs.detect_vwap_bounce(df, vwap)["signal"]


# ── Conviction ───────────────────────────────────────────────────────────────

def test_conviction_high_on_proven_trio():
    signals = {"vwap": {"score": 50}, "orb": {"score": 40}, "spike": {"score": 30}}
    assert hs.hft_conviction(signals) == "high"


def test_conviction_relaxed_without_trio():
    signals = {"range": {"score": 50}, "bounce": {"score": 40}, "spike": {"score": 0}}
    assert hs.hft_conviction(signals) == "relaxed"


# ── Confluence gating in score_hft_setup ─────────────────────────────────────

def test_single_signal_below_confluence_floor_scores_zero(monkeypatch):
    monkeypatch.setattr(hs, "HFT_MIN_CONFLUENCE", 2)
    signals = {"vwap": {"score": 60}, "trend": {}}   # only ONE core signal
    assert hs.score_hft_setup(signals) == 0


def test_two_signals_meet_relaxed_confluence(monkeypatch):
    monkeypatch.setattr(hs, "HFT_MIN_CONFLUENCE", 2)
    signals = {"range": {"score": 55}, "bounce": {"score": 45}, "trend": {}}
    assert hs.score_hft_setup(signals) > 0   # qualifies on the looser confluence


def test_proven_trio_scores_high(monkeypatch):
    monkeypatch.setattr(hs, "HFT_MIN_CONFLUENCE", 2)
    signals = {
        "vwap": {"score": 60}, "orb": {"score": 65}, "spike": {"score": 50},
        "trend": {"aligned_bullish": True},
    }
    assert hs.score_hft_setup(signals, direction="bullish") >= 60


def test_strict_confluence_blocks_relaxed(monkeypatch):
    # Setting the floor back to 3 reverts to (near) the original selectivity
    monkeypatch.setattr(hs, "HFT_MIN_CONFLUENCE", 3)
    signals = {"range": {"score": 55}, "bounce": {"score": 45}, "trend": {}}  # only 2 core
    assert hs.score_hft_setup(signals) == 0


# ── Prime session gate (ET timestamps) ─────────────────────────────────────────

def test_prime_session_morning_et():
    """10:30 AM ET is inside the prime window."""
    assert hs.is_prime_session(pd.Timestamp("2026-06-03 10:30:00"))


def test_prime_session_boundary_start():
    """9:45 AM ET is exactly the prime window start — should be included.
    (Widened from 10:00 in the Jun 2026 trade-frequency push: the 15-min
    opening range completes at 9:45, which is exactly when ORB breakouts fire.)"""
    assert hs.is_prime_session(pd.Timestamp("2026-06-03 09:45:00"))


def test_prime_session_early_et_blocked():
    """9:40 AM ET (opening range still forming) is before the window — blocked."""
    assert not hs.is_prime_session(pd.Timestamp("2026-06-03 09:40:00"))


def test_prime_session_late_et_blocked():
    """3:00 PM ET is after the 2:45 PM cutoff — blocked."""
    assert not hs.is_prime_session(pd.Timestamp("2026-06-03 15:00:00"))


def test_prime_session_boundary_end():
    """2:45 PM ET is exactly the prime window end — should be included."""
    assert hs.is_prime_session(pd.Timestamp("2026-06-03 14:45:00"))


# ── Strong bar signal ────────────────────────────────────────────────────────

def test_strong_bar_bullish_close_near_high():
    # Last bar: high=103, low=100, close=102.5 → quality=83% → bullish
    closes = [100.0, 100.0, 100.0, 100.0, 102.5]
    highs  = [100.5, 100.5, 100.5, 100.5, 103.0]
    lows   = [99.5,  99.5,  99.5,  99.5,  100.0]
    vols   = [1000,  1000,  1000,  1000,  2000]
    r = hs.detect_strong_bar(_df(highs, lows, closes, vols))
    assert r["signal"] and r["direction"] == "bullish"
    assert r["score"] > 0


def test_strong_bar_bearish_close_near_low():
    # Last bar: high=103, low=100, close=100.5 → quality=17% → bearish
    closes = [100.0, 100.0, 100.0, 100.0, 100.5]
    highs  = [100.5, 100.5, 100.5, 100.5, 103.0]
    lows   = [99.5,  99.5,  99.5,  99.5,  100.0]
    vols   = [1000,  1000,  1000,  1000,  2000]
    r = hs.detect_strong_bar(_df(highs, lows, closes, vols))
    assert r["signal"] and r["direction"] == "bearish"


def test_strong_bar_indecisive_close_at_midpoint():
    # Last bar: high=103, low=100, close=101.5 → quality=50% → no signal
    closes = [100.0, 100.0, 100.0, 100.0, 101.5]
    highs  = [100.5, 100.5, 100.5, 100.5, 103.0]
    lows   = [99.5,  99.5,  99.5,  99.5,  100.0]
    vols   = [1000] * 5
    r = hs.detect_strong_bar(_df(highs, lows, closes, vols))
    assert not r["signal"]


def test_strong_bar_direction_mismatch_zeroed_in_score():
    # Bearish strong bar should NOT boost a bullish setup score. Three core
    # signals so the setup clears the confluence floor on its own.
    base = {"vwap": {"score": 50}, "orb": {"score": 45}, "spike": {"score": 40}, "trend": {}}
    signals = {**base, "strong_bar": {"score": 40, "signal": True, "direction": "bearish"}}
    score_without_sb = hs.score_hft_setup(base, direction="bullish")
    score_with_sb = hs.score_hft_setup(signals, direction="bullish")
    # Conflicting strong_bar direction is zeroed; score should not increase
    assert score_with_sb <= score_without_sb + 1  # allow rounding


def test_strong_bar_aligned_direction_boosts_score():
    # Bullish strong bar on a bullish setup → score is higher than without it
    base = {"vwap": {"score": 50}, "orb": {"score": 45}, "spike": {"score": 40}, "trend": {}}
    with_sb = {**base, "strong_bar": {"score": 50, "signal": True, "direction": "bullish"}}
    assert hs.score_hft_setup(with_sb, direction="bullish") > hs.score_hft_setup(base, direction="bullish")


# ── Bidirectional (long + short) signals ─────────────────────────────────────

def _bidir(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", True)


def test_vwap_rejection_bearish_when_bidirectional(monkeypatch):
    _bidir(monkeypatch)
    closes = [101, 101, 101, 102, 99]   # last bar crosses VWAP(100) from above to below
    df = _df([c + 0.5 for c in closes], [c - 0.5 for c in closes], closes,
             [1000, 1000, 1000, 1000, 3000])
    vwap = pd.Series([100] * 5, index=df.index)
    r = hs.detect_vwap_reclaim(df, vwap)
    assert r["signal"] and r["direction"] == "bearish"


def test_vwap_rejection_suppressed_in_longonly(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", False)
    closes = [101, 101, 101, 102, 99]
    df = _df([c + 0.5 for c in closes], [c - 0.5 for c in closes], closes,
             [1000, 1000, 1000, 1000, 3000])
    vwap = pd.Series([100] * 5, index=df.index)
    assert not hs.detect_vwap_reclaim(df, vwap)["signal"]   # bearish branch off


def test_volume_spike_bearish_on_down_bar(monkeypatch):
    _bidir(monkeypatch)
    closes = [100] * 9 + [98]
    opens  = [100] * 9 + [100]          # last: open 100 > close 98 → down bar
    df = _df([c + 0.5 for c in closes], [c - 2 for c in closes], closes,
             [1000] * 9 + [5000], opens=opens)
    r = hs.detect_volume_spike(df)
    assert r["signal"] and r["direction"] == "bearish"


def test_volume_spike_down_bar_suppressed_in_longonly(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", False)
    closes = [100] * 9 + [98]
    opens  = [100] * 9 + [100]
    df = _df([c + 0.5 for c in closes], [c - 2 for c in closes], closes,
             [1000] * 9 + [5000], opens=opens)
    assert not hs.detect_volume_spike(df)["signal"]


def test_momentum_bearish_on_downtrend(monkeypatch):
    _bidir(monkeypatch)
    up   = [90 + i for i in range(8)]            # 90..97 rising (RSI high)
    down = [97 - 2 * i for i in range(1, 8)]     # 95,93,..,83 falling
    closes = up + down
    df = _df([c + 0.2 for c in closes], [c - 0.2 for c in closes], closes, [1000] * len(closes))
    r = hs.detect_momentum_burst(df)
    assert r["signal"] and r["direction"] == "bearish"


def test_vwap_reject_bearish_bounce(monkeypatch):
    _bidir(monkeypatch)
    closes = [99, 98.5, 98, 99, 99.4, 98.8]      # below VWAP(100), rallied to it, turned down
    highs  = [c + 0.3 for c in closes]
    highs[4] = 99.9                               # recent high tests VWAP from below
    lows   = [c - 0.3 for c in closes]
    df = _df(highs, lows, closes, [1000, 1000, 1000, 1200, 1500, 1800])
    vwap = pd.Series([100] * 6, index=df.index)
    r = hs.detect_vwap_bounce(df, vwap)
    assert r["signal"] and r["direction"] == "bearish"


# ── Direction resolution ─────────────────────────────────────────────────────

def test_resolve_direction_legacy_orb_priority(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", False)
    signals = {"orb": {"direction": "bearish", "score": 40},
               "range": {"direction": "bullish", "score": 50}}
    assert hs.resolve_direction(signals) == "bearish"   # ORB wins in legacy mode


def test_resolve_direction_legacy_defaults_bullish(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", False)
    assert hs.resolve_direction({"spike": {"direction": "none", "score": 0}}) == "bullish"


def test_resolve_direction_bidirectional_vote(monkeypatch):
    _bidir(monkeypatch)
    signals = {"vwap": {"direction": "bearish", "score": 60},
               "spike": {"direction": "bearish", "score": 40},
               "orb": {"direction": "bullish", "score": 50}}
    assert hs.resolve_direction(signals) == "bearish"   # 100 bear vs 50 bull


# ── Direction-consistent scoring & conviction (bidirectional mode) ───────────

def test_bidirectional_opposite_signals_below_floor(monkeypatch):
    _bidir(monkeypatch)
    # Two bullish core signals, but evaluated as a BEARISH setup → none align →
    # below the confluence floor → score 0.
    signals = {"vwap": {"score": 60, "direction": "bullish"},
               "orb": {"score": 50, "direction": "bullish"}, "trend": {}}
    assert hs.score_hft_setup(signals, direction="bearish") == 0


def test_bidirectional_aligned_bearish_scores_high(monkeypatch):
    _bidir(monkeypatch)
    signals = {"vwap": {"score": 60, "direction": "bearish"},
               "orb": {"score": 55, "direction": "bearish"},
               "spike": {"score": 50, "direction": "bearish"},
               "trend": {"aligned_bearish": True}}
    assert hs.score_hft_setup(signals, direction="bearish") >= 60


def test_legacy_scoring_ignores_direction(monkeypatch):
    monkeypatch.setattr(hs, "ENABLE_BIDIRECTIONAL_SIGNALS", False)
    # In long-only mode the bullish scores still count for a bearish setup
    # (legacy behavior preserved — direction filtering is bidirectional-only).
    # Three core signals so the setup clears the confluence floor.
    signals = {"vwap": {"score": 60, "direction": "bullish"},
               "orb": {"score": 50, "direction": "bullish"},
               "spike": {"score": 40, "direction": "bullish"}, "trend": {}}
    assert hs.score_hft_setup(signals, direction="bearish") > 0


def test_conviction_high_on_bearish_trio(monkeypatch):
    _bidir(monkeypatch)
    signals = {"vwap": {"score": 50, "direction": "bearish"},
               "orb": {"score": 40, "direction": "bearish"},
               "spike": {"score": 30, "direction": "bearish"}}
    assert hs.hft_conviction(signals, "bearish") == "high"


def test_conviction_relaxed_when_trio_directions_mixed(monkeypatch):
    _bidir(monkeypatch)
    # Trio all fire, but spike is bullish while the setup is bearish → not the
    # aligned proven trio → relaxed (half-size).
    signals = {"vwap": {"score": 50, "direction": "bearish"},
               "orb": {"score": 40, "direction": "bearish"},
               "spike": {"score": 30, "direction": "bullish"}}
    assert hs.hft_conviction(signals, "bearish") == "relaxed"
