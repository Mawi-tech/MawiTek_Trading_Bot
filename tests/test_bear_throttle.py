"""Tests for the bear-market risk throttle in risk_manager."""

import mawitek.core.risk_manager as rm
import mawitek.data.market_regime as mr


# ── _bear_throttle unit ──────────────────────────────────────────────────────

def test_throttle_noop_in_bull(monkeypatch):
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    assert rm._bear_throttle("pead") == (1.0, 1.0, None)


def test_throttle_derisks_in_bear(monkeypatch):
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    monkeypatch.setattr(rm, "BEAR_PAUSE_LONGS", False)
    size, cap, reject = rm._bear_throttle("pead")
    assert size == rm.BEAR_SIZE_MULT
    assert cap == rm.BEAR_POSITION_MULT
    assert reject is None          # de-risked, not paused


def test_throttle_pauses_only_long_directional_when_configured(monkeypatch):
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    monkeypatch.setattr(rm, "BEAR_PAUSE_LONGS", True)
    assert rm._bear_throttle("pead")[2] is not None                 # long → paused
    assert rm._bear_throttle("catalyst_long_call")[2] is not None   # long → paused
    assert rm._bear_throttle("iv_rank")[2] is None                  # premium-sell → only de-risked
    assert rm._bear_throttle("hft_intraday")[2] is None             # intraday → only de-risked


def test_throttle_master_switch_off(monkeypatch):
    monkeypatch.setattr(rm, "BEAR_REGIME_THROTTLE", False)
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    assert rm._bear_throttle("pead") == (1.0, 1.0, None)


def test_throttle_fails_open_on_regime_error(monkeypatch):
    def boom():
        raise RuntimeError("SPY data unavailable")
    monkeypatch.setattr(mr, "is_bear_market", boom)
    assert rm._bear_throttle("pead") == (1.0, 1.0, None)   # never blocks trading


# ── pre_trade_check integration ──────────────────────────────────────────────

def _stub_checks(monkeypatch, positions=0):
    monkeypatch.setattr(rm, "get_account_balance", lambda: {"total_equity": 100_000})
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda eq: (False, 0.0))
    monkeypatch.setattr(rm, "is_already_in_position", lambda t: False)
    monkeypatch.setattr(rm, "concentration_reject", lambda t: None)
    monkeypatch.setattr(rm, "_strategy_budget", lambda strat, eq, b: (b, None))
    monkeypatch.setattr(rm, "count_positions_by_type", lambda tt: positions)
    # Isolate the drawdown governor — these tests exercise the bear throttle, and
    # the real governor would otherwise persist drawdown_state.json into the cwd.
    monkeypatch.setattr(rm, "drawdown_governor", lambda eq: (1.0, None))


def test_pre_trade_check_bear_halves_budget(monkeypatch):
    _stub_checks(monkeypatch, positions=0)
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    monkeypatch.setattr(rm, "BEAR_PAUSE_LONGS", False)
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert res["approved"]
    assert res["budget"] == rm.get_position_size(100_000) * rm.BEAR_SIZE_MULT


def test_pre_trade_check_bear_tightens_position_cap(monkeypatch):
    # Swing cap 7 → int(7*0.6)=4 in a bear regime, so 4 open positions is full.
    _stub_checks(monkeypatch, positions=4)
    monkeypatch.setattr(mr, "is_bear_market", lambda: True)
    monkeypatch.setattr(rm, "BEAR_PAUSE_LONGS", False)
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert not res["approved"]
    assert "4/4" in res["reason"]


def test_pre_trade_check_bull_keeps_full_cap(monkeypatch):
    # Same 4 positions are fine in a bull regime (cap stays 7).
    _stub_checks(monkeypatch, positions=4)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert res["approved"]
    assert res["budget"] == rm.get_position_size(100_000)   # not throttled
