"""Tests for the intraday red-day detector (market_regime) and its risk gate
(risk_manager._red_day_throttle + pre_trade_check wiring)."""

import pytest

import market_regime as mr
import risk_manager as rm
import tradier_client as tc


@pytest.fixture(autouse=True)
def _fresh_cache():
    mr.clear_cache()
    yield
    mr.clear_cache()


def _status(state, chg=None):
    return {"state": state, "spy_chg_pct": chg, "detail": ""}


# ── classify_red_day (pure) ──────────────────────────────────────────────────

def test_classify_thresholds():
    assert mr.classify_red_day(0.5) == "ok"
    assert mr.classify_red_day(0.0) == "ok"
    assert mr.classify_red_day(-0.5) == "ok"           # above weak line
    assert mr.classify_red_day(-0.75) == "weak"        # inclusive
    assert mr.classify_red_day(-1.0) == "weak"
    assert mr.classify_red_day(-1.50) == "red"         # inclusive
    assert mr.classify_red_day(-3.2) == "red"


def test_classify_unknown_on_bad_input():
    assert mr.classify_red_day(None) == "unknown"
    assert mr.classify_red_day("garbage") == "unknown"
    assert mr.classify_red_day(float("nan")) == "unknown"


def test_classify_hysteresis_keeps_tripped_day_weak():
    # Tripped earlier; SPY recovers to -0.5 (above weak, below recover) → stay weak.
    assert mr.classify_red_day(-0.5, prev_state="weak") == "weak"
    # A genuine recovery above RED_DAY_RECOVER_PCT clears it.
    assert mr.classify_red_day(-0.3, prev_state="weak") == "ok"
    assert mr.classify_red_day(0.2, prev_state="red") == "ok"


def test_classify_red_relaxes_to_weak_not_ok():
    # Was red; drop eased to -1.0 — pause relaxes to throttle, not to normal.
    assert mr.classify_red_day(-1.0, prev_state="red") == "weak"


# ── intraday_market_status (live read, TTL cache, fail-open) ─────────────────

def test_status_reads_spy_quote(monkeypatch):
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -1.8}})
    s = mr.intraday_market_status()
    assert s["state"] == "red"
    assert s["spy_chg_pct"] == -1.8


def test_status_fails_open_on_error(monkeypatch):
    def boom(syms):
        raise RuntimeError("network down")
    monkeypatch.setattr(tc, "get_quote_details", boom)
    assert mr.intraday_market_status()["state"] == "unknown"


def test_status_unknown_when_no_quote(monkeypatch):
    # MOCK_MODE returns {} — must degrade to unknown, never raise.
    monkeypatch.setattr(tc, "get_quote_details", lambda syms: {})
    assert mr.intraday_market_status()["state"] == "unknown"


def test_status_is_ttl_cached(monkeypatch):
    calls = {"n": 0}

    def fake(syms):
        calls["n"] += 1
        return {"SPY": {"change_pct": -0.9}}

    monkeypatch.setattr(tc, "get_quote_details", fake)
    assert mr.intraday_market_status()["state"] == "weak"
    assert mr.intraday_market_status()["state"] == "weak"   # served from cache
    assert calls["n"] == 1


def test_status_refetches_after_ttl(monkeypatch):
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -1.6}})
    assert mr.intraday_market_status()["state"] == "red"
    # Age the cache past the TTL; the tripped state must persist via hysteresis
    # even though SPY has recovered to -0.6 (below the recovery line).
    mr._intraday_cache["ts"] -= mr.RED_DAY_TTL_SEC + 1
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -0.6}})
    assert mr.intraday_market_status()["state"] == "weak"


def test_hysteresis_survives_a_data_gap(monkeypatch):
    # weak → (data gap: unknown) → still weak once data returns at -0.5.
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -0.9}})
    assert mr.intraday_market_status()["state"] == "weak"
    mr._intraday_cache["ts"] -= mr.RED_DAY_TTL_SEC + 1
    monkeypatch.setattr(tc, "get_quote_details", lambda syms: {})
    assert mr.intraday_market_status()["state"] == "unknown"
    mr._intraday_cache["ts"] -= mr.RED_DAY_TTL_SEC + 1
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -0.5}})
    assert mr.intraday_market_status()["state"] == "weak"


def test_new_session_resets_hysteresis(monkeypatch):
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -2.0}})
    assert mr.intraday_market_status()["state"] == "red"
    # Pretend the cache is from yesterday: -0.5 must classify fresh → "ok".
    mr._intraday_cache["day"] = "1999-01-01"
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -0.5}})
    assert mr.intraday_market_status()["state"] == "ok"


def test_is_red_day_and_is_market_weak(monkeypatch):
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": -1.7}})
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    assert mr.is_red_day()
    assert mr.is_market_weak()


def test_is_market_weak_true_in_bear_regime_even_on_green_day(monkeypatch):
    monkeypatch.setattr(tc, "get_quote_details",
                        lambda syms: {"SPY": {"change_pct": 0.8}})
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    assert mr.is_market_weak()
    assert not mr.is_red_day()


# ── _red_day_throttle unit ───────────────────────────────────────────────────

def test_gate_noop_on_normal_tape(monkeypatch):
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("ok", 0.2))
    assert rm._red_day_throttle("pead") == (1.0, None)


def test_gate_halves_budget_on_weak_tape(monkeypatch):
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("weak", -0.9))
    assert rm._red_day_throttle("pead") == (rm.RED_DAY_SIZE_MULT, None)


def test_gate_rejects_on_red_tape(monkeypatch):
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("red", -2.1))
    mult, reject = rm._red_day_throttle("pead")
    assert mult == 0.0
    assert reject and "Red day" in reject


def test_gate_only_touches_gated_strategies(monkeypatch):
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("red", -2.1))
    assert rm._red_day_throttle("pead")[1] is not None
    assert rm._red_day_throttle("catalyst_long_call")[1] is not None
    assert rm._red_day_throttle("hft_intraday")[1] is not None   # long-only in prod
    assert rm._red_day_throttle("iv_rank") == (1.0, None)   # picks its own direction
    assert rm._red_day_throttle("bounce") == (1.0, None)    # the bear offense
    assert rm._red_day_throttle(None) == (1.0, None)


def test_gate_master_switch_off(monkeypatch):
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("red", -2.1))
    monkeypatch.setattr(rm, "RED_DAY_GATE", False)
    assert rm._red_day_throttle("pead") == (1.0, None)


def test_gate_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("quote unavailable")
    monkeypatch.setattr(mr, "intraday_market_status", boom)
    assert rm._red_day_throttle("pead") == (1.0, None)
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("unknown"))
    assert rm._red_day_throttle("pead") == (1.0, None)


# ── pre_trade_check integration ──────────────────────────────────────────────

def _stub_checks(monkeypatch, positions=0):
    monkeypatch.setattr(rm, "get_account_balance", lambda: {"total_equity": 100_000})
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda eq: (False, 0.0))
    monkeypatch.setattr(rm, "is_already_in_position", lambda t: False)
    monkeypatch.setattr(rm, "concentration_reject", lambda t: None)
    monkeypatch.setattr(rm, "_strategy_budget", lambda strat, eq, b: (b, None))
    monkeypatch.setattr(rm, "count_positions_by_type", lambda tt: positions)
    monkeypatch.setattr(rm, "drawdown_governor", lambda eq: (1.0, None))


def test_pre_trade_check_weak_day_halves_budget(monkeypatch):
    _stub_checks(monkeypatch)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("weak", -0.9))
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert res["approved"]
    assert res["budget"] == rm.get_position_size(100_000) * rm.RED_DAY_SIZE_MULT


def test_pre_trade_check_red_day_rejects_longs(monkeypatch):
    _stub_checks(monkeypatch)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("red", -2.0))
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert not res["approved"]
    assert "Red day" in res["reason"]


def test_pre_trade_check_red_day_lets_bounce_and_iv_rank_through(monkeypatch):
    _stub_checks(monkeypatch)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("red", -2.0))
    assert rm.pre_trade_check("AAPL", strategy="bounce")["approved"]
    assert rm.pre_trade_check("AAPL", strategy="iv_rank")["approved"]


def test_bear_and_red_day_compose_with_min_not_product(monkeypatch):
    # Both fire at 0.5 — the budget must be halved ONCE (min), not quartered.
    _stub_checks(monkeypatch)
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    monkeypatch.setattr(rm, "BEAR_PAUSE_LONGS", False)
    monkeypatch.setattr(mr, "intraday_market_status", lambda: _status("weak", -0.9))
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert res["approved"]
    assert res["budget"] == rm.get_position_size(100_000) * 0.5
