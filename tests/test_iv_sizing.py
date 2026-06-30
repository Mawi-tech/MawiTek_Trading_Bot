"""Tests for IV-aware position sizing in the risk manager (the IV → trade wiring)."""

import mawitek.core.risk_manager as rm


def _ctx(regime, rank=None, ratio=None):
    return {"regime": regime, "iv_rank": rank, "iv_hv_ratio": ratio}


# ─── _iv_mult_from_ctx (pure) ───────────────────────────────────────────────────

def test_rich_iv_desizes_long_premium_buyer():
    assert rm._iv_mult_from_ctx("catalyst_long_call", _ctx("rich")) == rm.IV_RICH_MULT
    assert rm._iv_mult_from_ctx("pead", _ctx("rich")) == rm.IV_RICH_MULT
    assert rm._iv_mult_from_ctx("bounce", _ctx("rich")) == rm.IV_RICH_MULT


def test_very_rich_iv_desizes_more():
    # By rank ...
    assert rm._iv_mult_from_ctx("pead", _ctx("rich", rank=90)) == rm.IV_VERY_RICH_MULT
    # ... or by IV/HV ratio
    assert rm._iv_mult_from_ctx("pead", _ctx("rich", ratio=1.8)) == rm.IV_VERY_RICH_MULT


def test_cheap_or_normal_iv_is_full_size():
    assert rm._iv_mult_from_ctx("catalyst_long_call", _ctx("cheap")) == 1.0
    assert rm._iv_mult_from_ctx("catalyst_long_call", _ctx("normal")) == 1.0


def test_iv_rank_strategy_is_not_iv_sized_here():
    # iv_rank has its OWN IV logic — the risk-manager IV sizing must not touch it.
    assert rm._iv_mult_from_ctx("iv_rank", _ctx("rich", rank=95)) == 1.0


def test_hft_is_not_iv_sized():
    # Intraday gamma play — insensitive to the 30-DTE IV level.
    assert rm._iv_mult_from_ctx("hft_intraday", _ctx("rich")) == 1.0


def test_missing_iv_context_is_full_size():
    assert rm._iv_mult_from_ctx("pead", None) == 1.0


def test_config_sane():
    assert rm.IV_AWARE_SIZING is True
    assert 0 < rm.IV_VERY_RICH_MULT < rm.IV_RICH_MULT < 1.0
    assert "iv_rank" not in rm.IV_SIZED_STRATEGIES
    assert "hft_intraday" not in rm.IV_SIZED_STRATEGIES


# ─── _iv_size_mult (fails open) ──────────────────────────────────────────────────

def test_iv_size_mult_fails_open(monkeypatch):
    import mawitek.data.iv_provider as iv_provider
    monkeypatch.setattr(iv_provider, "iv_context", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rm._iv_size_mult("pead", "AAA") == 1.0     # error → no change


def test_iv_size_mult_applies_when_rich(monkeypatch):
    import mawitek.data.iv_provider as iv_provider
    monkeypatch.setattr(iv_provider, "iv_context", lambda t: _ctx("rich"))
    assert rm._iv_size_mult("pead", "AAA") == rm.IV_RICH_MULT
    # non-IV-sized strategy short-circuits without even calling iv_context
    assert rm._iv_size_mult("iv_rank", "AAA") == 1.0
