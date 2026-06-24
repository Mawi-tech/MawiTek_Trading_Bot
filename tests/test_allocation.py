"""Tests for per-strategy capital allocation in risk_manager."""

import risk_manager as rm


def test_strategy_budget_uncapped_when_unknown_strategy():
    # Unknown strategy → no cap applied, returns base budget unchanged
    budget, reject = rm._strategy_budget("not_a_strategy", 100_000, 2000)
    assert reject is None
    assert budget == 2000


def test_strategy_budget_none_strategy_passthrough():
    budget, reject = rm._strategy_budget(None, 100_000, 1500)
    assert reject is None
    assert budget == 1500


def test_strategy_budget_clamps_to_remaining(monkeypatch):
    # catalyst cap 40% of 100k = 40k; pretend 39k already deployed → 1k remaining
    monkeypatch.setattr(rm, "deployed_capital_by_strategy", lambda: {"catalyst_long_call": 39_000})
    budget, reject = rm._strategy_budget("catalyst_long_call", 100_000, 5000)
    assert reject is None
    assert budget == 1000  # clamped to remaining allocation


def test_strategy_budget_rejects_when_full(monkeypatch):
    # 40k deployed vs 40k cap → no room
    monkeypatch.setattr(rm, "deployed_capital_by_strategy", lambda: {"catalyst_long_call": 40_000})
    budget, reject = rm._strategy_budget("catalyst_long_call", 100_000, 5000)
    assert budget == 0
    assert reject is not None and "allocation full" in reject


def test_strategy_budget_unrestricted_when_nothing_deployed(monkeypatch):
    monkeypatch.setattr(rm, "deployed_capital_by_strategy", lambda: {})
    budget, reject = rm._strategy_budget("hft_intraday", 100_000, 1000)
    assert reject is None
    # hft cap is 20% of 100k = 20k, base budget 1000 < cap → unchanged
    assert budget == 1000


def test_deployed_capital_reads_positions(tmp_path, monkeypatch):
    # Point position loader at a synthetic book
    import position_manager as pm
    fake = {
        "AAPL260101C00200000": {"strategy": "catalyst_long_call", "entry_price": 4.0, "quantity": 2},
        "NVDA260101C00800000": {"strategy": "catalyst_long_call", "entry_price": 10.0, "quantity": 1},
    }
    monkeypatch.setattr(pm, "load_positions", lambda: fake)
    # No hft file
    monkeypatch.chdir(tmp_path)
    deployed = rm.deployed_capital_by_strategy()
    # 4*2*100 + 10*1*100 = 800 + 1000 = 1800
    assert deployed.get("catalyst_long_call") == 1800
