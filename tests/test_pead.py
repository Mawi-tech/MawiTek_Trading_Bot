"""Tests for Strategy 4 (post-earnings / news-drift): detector + risk wiring."""

import json

import pandas as pd

import pead_scanner as pead
import risk_manager as rm


# ── Synthetic daily frames ───────────────────────────────────────────────────

def _make_df(trend_per_day: float, event_idx: int, event_move: float,
             hold_move: float, n: int = 60, event_vol_mult: float = 4.0):
    """Build a daily OHLCV frame: a noisy trend, then a gap event + drift.

    trend_per_day: compounding drift of the baseline (sets the SMA50 slope).
    event_move:    the gap return on event_idx (e.g. +0.08).
    hold_move:     return from the event bar to the last bar (the drift/fade).
    """
    idx = pd.bdate_range("2025-01-01", periods=n)
    base = [100 * ((1 + trend_per_day) ** i) for i in range(n)]
    # Deterministic alternating noise → realistic non-zero daily vol baseline.
    prices = [base[i] * (1 + 0.007 * (1 if i % 2 else -1)) for i in range(n)]
    vols = [1_000_000] * n

    prices[event_idx] = prices[event_idx - 1] * (1 + event_move)
    # Last bar drifts/fades from the event close.
    prices[n - 1] = prices[event_idx] * (1 + hold_move)
    vols[event_idx] = int(1_000_000 * event_vol_mult)

    return pd.DataFrame({
        "Open":   prices,
        "High":   [p * 1.01 for p in prices],
        "Low":    [p * 0.99 for p in prices],
        "Close":  prices,
        "Volume": vols,
    }, index=idx)


# ── Detector ─────────────────────────────────────────────────────────────────

def test_bullish_gap_in_uptrend_qualifies():
    df = _make_df(trend_per_day=0.004, event_idx=58, event_move=0.08,
                  hold_move=0.005, event_vol_mult=4.0)
    setup = pead.detect_drift(df)
    assert setup is not None
    assert setup["direction"] == "bullish"
    assert setup["trend_aligned"] is True
    assert setup["days_since"] == 1
    assert setup["conviction"] == "high"          # big gap + heavy volume
    assert setup["setup_score"] >= pead.MIN_SETUP_SCORE


def test_bearish_gap_qualifies_when_regime_allows():
    # Bearish only trades when the caller passes bearish_allowed=True (bear regime).
    df = _make_df(trend_per_day=-0.004, event_idx=58, event_move=-0.08,
                  hold_move=-0.005, event_vol_mult=4.0)
    setup = pead.detect_drift(df, bearish_allowed=True)
    assert setup is not None
    assert setup["direction"] == "bearish"
    assert setup["trend_aligned"] is True


def test_counter_trend_gap_rejected_by_trend_gate():
    # Up-gap but in a DOWNtrend → trend gate rejects it (independent of direction).
    df = _make_df(trend_per_day=-0.004, event_idx=58, event_move=0.08, hold_move=0.005)
    assert pead.detect_drift(df, bearish_allowed=True) is None


def test_bearish_suppressed_by_default():
    # Default (bearish_allowed=False, e.g. a bull regime): no short trades.
    df = _make_df(trend_per_day=-0.004, event_idx=58, event_move=-0.08,
                  hold_move=-0.005, event_vol_mult=4.0)
    assert pead.detect_drift(df) is None


# ── Market-regime gate ───────────────────────────────────────────────────────

def _spy_series(values):
    return pd.Series(values, index=pd.bdate_range("2023-01-01", periods=len(values)))


def test_is_bear_regime_true_below_sma():
    # Long climb, then a sharp drop so the last close sits below the 200d SMA.
    vals = [100 + i * 0.5 for i in range(200)] + [199.5 - i * 5 for i in range(30)]
    assert pead.is_bear_regime(_spy_series(vals)) is True


def test_is_bear_regime_false_above_sma():
    vals = [100 + i * 0.5 for i in range(230)]     # steady uptrend → above SMA200
    assert pead.is_bear_regime(_spy_series(vals)) is False


def test_is_bear_regime_false_when_insufficient_history():
    assert pead.is_bear_regime(_spy_series([100] * 50)) is False


def test_strategy_ships_long_only():
    # Regime-gated shorts were built + backtested over 4yr (incl. 2022) and
    # FAILED (8% win, -$12.7k). The shipped default is pure long-only.
    assert pead.BEARISH_REGIME_FILTER is False
    assert pead.bearish_allowed_now() is False   # never enables shorts while off


def test_faded_move_not_traded():
    # Gap up, then fully retrace back below the pre-event price → no drift.
    df = _make_df(trend_per_day=0.004, event_idx=58, event_move=0.08,
                  hold_move=-0.09)
    assert pead.detect_drift(df) is None


def test_calm_data_has_no_event():
    # Gentle uptrend, no gap (tiny "event") → nothing qualifies.
    df = _make_df(trend_per_day=0.004, event_idx=58, event_move=0.005,
                  hold_move=0.002, event_vol_mult=1.0)
    assert pead.detect_drift(df) is None


def test_moderate_gap_is_relaxed_conviction():
    df = _make_df(trend_per_day=0.004, event_idx=58, event_move=0.05,
                  hold_move=0.003, event_vol_mult=2.2)
    setup = pead.detect_drift(df)
    assert setup is not None
    assert setup["conviction"] == "relaxed"       # below the high-conviction bar


def test_inverse_etf_excluded():
    df = _make_df(trend_per_day=0.004, event_idx=58, event_move=0.08, hold_move=0.005)
    # scan_ticker should skip inverse ETFs before any network call.
    assert pead.scan_ticker("SQQQ") is None


# ── Daily-bar cache (live scan perf) ─────────────────────────────────────────

def test_daily_cache_reuses_within_ttl(monkeypatch):
    import market_data
    pead.clear_daily_cache()
    calls = {"n": 0}

    def fake(ticker, days=120):
        calls["n"] += 1
        return _make_df(0.004, 58, 0.08, 0.005)

    monkeypatch.setattr(market_data, "get_daily_bars", fake)
    monkeypatch.setattr(pead.time, "time", lambda: 1000.0)
    pead._fetch_daily_cached("AAA", 120)
    pead._fetch_daily_cached("AAA", 120)
    assert calls["n"] == 1          # second call served from cache


def test_daily_cache_skips_empty(monkeypatch):
    import market_data
    pead.clear_daily_cache()
    calls = {"n": 0}

    def fake(ticker, days=120):
        calls["n"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(market_data, "get_daily_bars", fake)
    monkeypatch.setattr(pead.time, "time", lambda: 1000.0)
    pead._fetch_daily_cached("AAA", 120)
    pead._fetch_daily_cached("AAA", 120)
    assert calls["n"] == 2          # empties are never cached


# ── Risk-manager wiring ──────────────────────────────────────────────────────

def test_pead_is_a_swing_strategy():
    assert "pead" in rm.SWING_STRATEGIES
    assert rm.trade_type_for("pead") == "swing"


def test_pead_has_capital_allocation():
    # PEAD got the lion's share of retired catalyst's old 40% (it's the
    # positive-EV post-earnings successor).
    assert rm.STRATEGY_ALLOCATION_PCT.get("pead") == 0.35
    assert "catalyst_long_call" not in rm.STRATEGY_ALLOCATION_PCT   # retired
    # Active strategies (iv_rank 35 / hft 25 / pead 35 / bounce 15) deliberately
    # sum to >1.0 — not every strategy is fully deployed at once, and the
    # per-trade-type position caps prevent over-commitment in practice.
    s = sum(rm.STRATEGY_ALLOCATION_PCT.values())
    assert 1.0 <= s <= 1.5


def test_swing_cap_has_room_for_pead():
    # MAX_SWING_POSITIONS covers catalyst + iv_rank + pead + bounce. Was 7
    # before the bounce strategy was added; bumped to 8 to give bounce its slot
    # without crowding out the other swings.
    assert rm.MAX_SWING_POSITIONS == 8


def test_swing_count_includes_pead_book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open("open_positions.json", "w") as f:
        json.dump({"SYM1": {}}, f)                    # 1 catalyst
    with open("pead_positions.json", "w") as f:
        json.dump([{"option_symbol": "A"}, {"option_symbol": "B"}], f)  # 2 pead
    assert rm.count_positions_by_type("swing") == 3   # 1 + 2


def test_pead_positions_count_toward_concentration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Three megacap-growth names held via the PEAD book → cluster is full.
    with open("pead_positions.json", "w") as f:
        json.dump([{"underlying": "AAPL"}, {"underlying": "MSFT"},
                   {"underlying": "NVDA"}], f)
    assert rm.concentration_reject("META") is not None   # 4th correlated name blocked
    assert rm.concentration_reject("XOM") is None        # unrelated cluster is fine
