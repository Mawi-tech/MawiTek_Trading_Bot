"""Tests for scanner hit-rate tracking (forward returns + edge aggregation)."""

import datetime

import mawitek.analysis.setup_tracker as st
from mawitek.infra.utils import now_est


def _aged(hours: float) -> str:
    return (now_est() - datetime.timedelta(hours=hours)).isoformat()


# ─── track_setups ───────────────────────────────────────────────────────────────

def test_first_sighting_stamps_ref_price():
    setups = [{"ticker": "AAA", "setup_score": 70, "trade_style": "swing", "found_at": _aged(1)}]
    st.track_setups(setups, quotes={"AAA": 100.0})
    assert setups[0]["ref_price"] == 100.0
    assert setups[0]["forward_return_pct"] == 0.0   # ref == current on first sight
    assert "ref_at" in setups[0]


def test_bullish_forward_return_is_favorable_positive():
    setups = [{"ticker": "AAA", "setup_score": 70, "trade_style": "swing",
               "ref_price": 100.0, "ref_at": _aged(2)}]
    st.track_setups(setups, quotes={"AAA": 105.0})
    assert setups[0]["forward_return_pct"] == 5.0   # +5% up move on a bullish setup = +5


def test_bearish_setup_favorable_when_underlying_drops():
    setups = [{"ticker": "BBB", "setup_score": 80, "trade_style": "day",
               "direction": "bearish", "ref_price": 100.0, "ref_at": _aged(1)}]
    st.track_setups(setups, quotes={"BBB": 95.0})
    assert setups[0]["forward_return_pct"] == 5.0   # -5% underlying, bearish → +5 favorable


def test_finalizes_win_after_horizon():
    # swing horizon is 5 days; age the ref past it.
    setups = [{"ticker": "AAA", "setup_score": 70, "trade_style": "swing",
               "ref_price": 100.0, "ref_at": _aged(5 * 24 + 1)}]
    st.track_setups(setups, quotes={"AAA": 104.0})   # +4% ≥ 2% threshold → win
    assert setups[0]["perf_finalized"] is True
    assert setups[0]["outcome"] == "win"
    assert setups[0]["outcome_return_pct"] == 4.0


def test_finalizes_loss_and_flat_by_threshold():
    loss = [{"ticker": "L", "trade_style": "day", "ref_price": 100.0, "ref_at": _aged(7)}]
    flat = [{"ticker": "F", "trade_style": "day", "ref_price": 100.0, "ref_at": _aged(7)}]
    st.track_setups(loss, quotes={"L": 97.0})   # -3% ≤ -2% → loss
    st.track_setups(flat, quotes={"F": 101.0})  # +1% within ±2% → flat
    assert loss[0]["outcome"] == "loss"
    assert flat[0]["outcome"] == "flat"


def test_finalized_setup_not_re_evaluated():
    setups = [{"ticker": "AAA", "perf_finalized": True, "outcome": "win",
               "ref_price": 100.0, "outcome_return_pct": 5.0}]
    st.track_setups(setups, quotes={"AAA": 50.0})  # would change it if re-evaluated
    assert setups[0]["outcome_return_pct"] == 5.0  # untouched


def test_missing_quote_skips_gracefully():
    setups = [{"ticker": "AAA", "trade_style": "swing", "found_at": _aged(1)}]
    st.track_setups(setups, quotes={})             # no quote available
    assert "ref_price" not in setups[0]


# ─── scanner_performance aggregation ────────────────────────────────────────────

def _final(ticker, score, outcome, ret):
    return {"ticker": ticker, "setup_score": score, "perf_finalized": True,
            "outcome": outcome, "outcome_return_pct": ret}


def test_scanner_performance_overall_and_buckets():
    setups = [
        _final("A", 80, "win",  6.0),
        _final("B", 78, "win",  3.0),
        _final("C", 65, "loss", -4.0),
        _final("D", 50, "flat",  0.5),
        {"ticker": "E", "setup_score": 90, "forward_return_pct": 1.0},  # still tracking
    ]
    perf = st.scanner_performance(setups)
    assert perf["finalized"] == 4
    assert perf["open"] == 1
    assert perf["wins"] == 2 and perf["losses"] == 1
    # 2 wins / 3 decided (flat doesn't count in hit rate)
    assert perf["hit_rate"] == round(2 / 3 * 100, 1)
    # high bucket (75+) = A,B both wins → 100% hit
    assert perf["by_score"]["75+"]["hit_rate"] == 100.0
    assert perf["by_score"]["75+"]["n"] == 2


def test_scanner_performance_empty_is_safe():
    perf = st.scanner_performance([])
    assert perf["finalized"] == 0 and perf["hit_rate"] is None
