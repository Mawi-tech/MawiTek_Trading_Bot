"""Tests for runtime control commands (bot_control) and their effect on
pre_trade_check. Flatten is NOT exercised here (it would hit the broker)."""

import pytest

import bot_control as bc
import risk_manager as rm
import market_regime as mr


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """Isolated cwd so control_state.json never touches real state."""
    monkeypatch.chdir(tmp_path)
    yield


# ── control state ────────────────────────────────────────────────────────────

def test_defaults_when_no_file():
    st = bc.status()
    assert st["manual_halt"] is False
    assert st["paused_strategies"] == []


def test_halt_and_resume():
    st = bc.halt("news risk")
    assert st["manual_halt"] is True and st["halt_reason"] == "news risk"
    assert bc.control_block_reason("iv_rank") is not None
    bc.resume()
    assert bc.status()["manual_halt"] is False
    assert bc.control_block_reason("iv_rank") is None


def test_pause_and_unpause_strategy():
    bc.pause_strategy("hft_intraday")
    assert "hft_intraday" in bc.status()["paused_strategies"]
    assert bc.control_block_reason("hft_intraday") is not None
    assert bc.control_block_reason("iv_rank") is None       # only the paused one
    bc.resume_strategy("hft_intraday")
    assert bc.control_block_reason("hft_intraday") is None


def test_pause_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        bc.pause_strategy("not_a_strategy")


def test_control_block_reason_fails_open(monkeypatch):
    # An unreadable control file must NEVER block trading.
    monkeypatch.setattr(bc, "load_control", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert bc.control_block_reason("iv_rank") is None


def test_paused_list_sanitized(monkeypatch):
    # Unknown names in a hand-edited file are dropped on read.
    from state_io import atomic_write_json
    atomic_write_json("control_state.json",
                      {"manual_halt": False, "paused_strategies": ["iv_rank", "junk"]})
    assert bc.status()["paused_strategies"] == ["iv_rank"]


# ── pre_trade_check integration ──────────────────────────────────────────────

def _stub(monkeypatch):
    monkeypatch.setattr(rm, "get_account_balance", lambda: {"total_equity": 100_000})
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda eq: (False, 0.0))
    monkeypatch.setattr(rm, "is_already_in_position", lambda t: False)
    monkeypatch.setattr(rm, "concentration_reject", lambda t: None)
    monkeypatch.setattr(rm, "_strategy_budget", lambda s, e, b: (b, None))
    monkeypatch.setattr(rm, "count_positions_by_type", lambda tt: 0)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)


def test_manual_halt_blocks_pre_trade_check(monkeypatch):
    _stub(monkeypatch)
    bc.halt("manual")
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert not res["approved"] and "halt" in res["reason"].lower()


def test_paused_strategy_blocks_only_itself(monkeypatch):
    _stub(monkeypatch)
    bc.pause_strategy("hft_intraday")
    blocked = rm.pre_trade_check("AAPL", strategy="hft_intraday")
    assert not blocked["approved"] and "paused" in blocked["reason"].lower()
    ok = rm.pre_trade_check("AAPL", strategy="pead")
    assert ok["approved"]


def test_retired_strategy_blocks_new_entries(monkeypatch):
    # Catalyst is retired (negative-EV) — pre_trade_check must refuse new entries
    # even though everything else (slots, equity, regime) is fine.
    _stub(monkeypatch)
    res = rm.pre_trade_check("AAPL", strategy="catalyst_long_call")
    assert not res["approved"] and "retired" in res["reason"].lower()


def test_paused_strategy_blocks_new_entries(monkeypatch):
    # The PAUSED_STRATEGIES mechanism blocks new entries (no strategy is paused
    # by default now, so monkeypatch one in to exercise the guard).
    _stub(monkeypatch)
    monkeypatch.setattr(rm, "PAUSED_STRATEGIES", {"iv_rank"})
    res = rm.pre_trade_check("AAPL", strategy="iv_rank")
    assert not res["approved"] and "paused" in res["reason"].lower()
