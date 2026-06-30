"""Tests for the IV provider's pure helpers + history accumulation."""

import math

import mawitek.data.iv_provider as iv


# ─── compute_hv ─────────────────────────────────────────────────────────────────

def test_compute_hv_flat_series_is_zero():
    assert iv.compute_hv([100.0] * 30) == 0.0


def test_compute_hv_annualizes():
    # Constant +1%/day for 25 days → tiny return std, annualized × sqrt(252).
    closes = [100.0]
    for _ in range(25):
        closes.append(closes[-1] * 1.01)
    hv = iv.compute_hv(closes, window=20)
    assert hv is not None and hv >= 0.0          # all returns equal → ~0 vol


def test_compute_hv_too_short_is_none():
    assert iv.compute_hv([100, 101, 102]) is None


def test_compute_hv_reasonable_magnitude():
    # Alternating ±2% daily → meaningful annualized vol (well above 50%).
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.02 if i % 2 == 0 else 0.98))
    hv = iv.compute_hv(closes, window=20)
    assert hv is not None and hv > 0.3


# ─── iv_rank_from_history / percentile ──────────────────────────────────────────

def test_iv_rank_needs_min_history():
    assert iv.iv_rank_from_history(0.5, [0.4, 0.6]) is None        # < MIN_IV_HISTORY


def test_iv_rank_positions_in_range():
    hist = [0.20, 0.30, 0.40, 0.50, 0.60, 0.25, 0.35, 0.45, 0.55, 0.22]  # min .20 max .60
    assert iv.iv_rank_from_history(0.60, hist) == 100.0
    assert iv.iv_rank_from_history(0.20, hist) == 0.0
    assert iv.iv_rank_from_history(0.40, hist) == 50.0


def test_iv_percentile_counts_below():
    hist = [0.1 * i for i in range(1, 11)]        # 0.1 .. 1.0 (10 values)
    assert iv.iv_percentile_from_history(0.5, hist) == 50.0
    assert iv.iv_percentile_from_history(1.0, hist) == 100.0


# ─── classify_iv ────────────────────────────────────────────────────────────────

def test_classify_prefers_rank():
    assert iv.classify_iv(0.5, 70) == "rich"      # rank says rich even if ratio low
    assert iv.classify_iv(2.0, 10) == "cheap"     # rank says cheap even if ratio high
    assert iv.classify_iv(1.0, 40) == "normal"


def test_classify_falls_back_to_ratio():
    assert iv.classify_iv(1.5, None) == "rich"
    assert iv.classify_iv(0.8, None) == "cheap"
    assert iv.classify_iv(1.1, None) == "normal"
    assert iv.classify_iv(None, None) == "n/a"


# ─── record_iv accumulation ─────────────────────────────────────────────────────

def test_record_iv_dedups_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(iv, "IV_HISTORY_FILE", str(tmp_path / "ivh.json"))
    iv.record_iv("AAA", 0.40)
    series = iv.record_iv("AAA", 0.45)            # same ET day → replaces, not appends
    assert series == [0.45]
