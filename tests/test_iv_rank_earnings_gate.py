"""IV-rank must not sell premium across an earnings date it can't stop out of."""

import datetime

import iv_rank_bot as ivr

TODAY = datetime.date(2026, 10, 5)
EXP = (TODAY + datetime.timedelta(days=30)).isoformat()   # forced exit at EXP − 7d


def _wire(monkeypatch, earnings):
    calls = {"chain": 0}

    def chain(t, e):
        calls["chain"] += 1
        return []

    monkeypatch.setattr(ivr, "today_est", lambda: TODAY)
    monkeypatch.setattr(ivr, "get_options_expirations", lambda t: [EXP])
    monkeypatch.setattr(ivr, "get_options_chain", chain)
    monkeypatch.setattr(ivr, "_next_earnings_date", earnings)
    return calls


def test_skips_sale_when_earnings_inside_hold(monkeypatch):
    calls = _wire(monkeypatch, lambda t: TODAY + datetime.timedelta(days=10))
    assert ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium") is None
    assert calls["chain"] == 0          # gated before spending a chain fetch


def test_earnings_on_the_last_held_day_is_gated(monkeypatch):
    calls = _wire(monkeypatch, lambda t: TODAY + datetime.timedelta(days=23))
    assert ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium") is None
    assert calls["chain"] == 0


def test_earnings_after_the_forced_exit_is_allowed(monkeypatch):
    calls = _wire(monkeypatch, lambda t: TODAY + datetime.timedelta(days=24))
    ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium")
    assert calls["chain"] == 1


def test_unknown_or_failed_lookup_fails_open(monkeypatch):
    calls = _wire(monkeypatch, lambda t: None)
    ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium")

    def boom(t):
        raise RuntimeError("provider down")

    monkeypatch.setattr(ivr, "_next_earnings_date", boom)
    ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium")
    assert calls["chain"] == 2


def test_buy_premium_is_not_gated(monkeypatch):
    calls = _wire(monkeypatch, lambda t: TODAY + datetime.timedelta(days=10))
    ivr.select_credit_spread_legs("COHR", 100.0, "buy_premium")
    assert calls["chain"] == 1


def test_gate_can_be_switched_off(monkeypatch):
    calls = _wire(monkeypatch, lambda t: TODAY + datetime.timedelta(days=10))
    monkeypatch.setattr(ivr, "SKIP_EARNINGS_IN_HOLD", False)
    ivr.select_credit_spread_legs("COHR", 100.0, "sell_premium")
    assert calls["chain"] == 1
