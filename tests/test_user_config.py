"""Tests for account-size tiers + dashboard-writable risk config (user_config.py)
and its wiring into risk_manager."""

import pytest

import user_config as uc
import risk_manager as rm
import market_regime as mr


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """Each test runs in an isolated cwd with no user_config.json and a fresh
    cache, so tier resolution is deterministic and never sees production state."""
    monkeypatch.chdir(tmp_path)
    uc._cache["mtime"] = None
    uc._cache["raw"] = None
    yield


# ── Tier selection by equity ─────────────────────────────────────────────────

def test_select_tier_boundaries():
    assert uc.select_tier(30_000) == "standard"
    assert uc.select_tier(25_000) == "standard"      # inclusive floor
    assert uc.select_tier(24_999) == "small"
    assert uc.select_tier(5_000) == "small"          # inclusive floor
    assert uc.select_tier(4_999) == "micro"
    assert uc.select_tier(0) == "micro"
    assert uc.select_tier(-100) == "micro"           # garbage → smallest


def test_select_tier_handles_bad_input():
    assert uc.select_tier(None) == "micro"
    assert uc.select_tier("not a number") == "micro"


# ── Keep-it-as-it-is guarantee ───────────────────────────────────────────────

def test_standard_tier_equals_risk_manager_constants():
    """The 'standard' preset MUST mirror the live risk_manager defaults, so a
    normal account behaves identically with the tier layer in place."""
    s = uc.TIER_PRESETS["standard"]
    assert s["risk_per_trade_pct"]    == rm.RISK_PER_TRADE_PCT
    assert s["max_position_size_pct"] == rm.MAX_POSITION_SIZE_PCT
    assert s["daily_loss_limit_pct"]  == rm.DAILY_LOSS_LIMIT_PCT
    assert s["max_swing_positions"]   == rm.MAX_SWING_POSITIONS
    assert s["max_day_positions"]     == rm.MAX_DAY_POSITIONS
    assert s["strategy_allocation_pct"] == rm.STRATEGY_ALLOCATION_PCT
    # All KNOWN_STRATEGIES are enabled EXCEPT retired (catalyst) and paused (hft).
    assert (set(s["enabled_strategies"])
            == set(uc.KNOWN_STRATEGIES) - rm.RETIRED_STRATEGIES - rm.PAUSED_STRATEGIES)


def test_effective_config_no_file_is_auto_and_matches_constants():
    cfg = uc.effective_config(100_000)
    assert cfg["tier"] == "standard"
    assert cfg["tier_source"] == "auto"
    assert cfg["risk_per_trade_pct"] == rm.RISK_PER_TRADE_PCT
    assert cfg["max_swing_positions"] == rm.MAX_SWING_POSITIONS


def test_small_account_auto_selects_small_tier():
    cfg = uc.effective_config(8_000)
    assert cfg["tier"] == "small"
    assert cfg["max_day_positions"] == 0          # no day-trading (PDT)
    assert "hft_intraday" not in cfg["enabled_strategies"]
    assert "catalyst_long_call" not in cfg["enabled_strategies"]


# ── Clamping / safety (this file is written from a web form) ──────────────────

def test_risk_per_trade_clamped_to_ceiling():
    uc.save_user_config({"tier": "auto", "overrides": {"risk_per_trade_pct": 0.5}})
    assert uc.effective_config(100_000)["risk_per_trade_pct"] == 0.10   # 10% cap


def test_daily_loss_halt_cannot_be_disabled():
    # Try to zero out the halt — must clamp to the 1% floor, never 0.
    uc.save_user_config({"tier": "auto", "overrides": {"daily_loss_limit_pct": 0}})
    assert uc.effective_config(100_000)["daily_loss_limit_pct"] == 0.01


def test_unknown_keys_and_strategies_dropped():
    saved = uc.save_user_config({
        "tier": "auto",
        "overrides": {
            "risk_per_trade_pct": 0.04,
            "not_a_real_field": 123,
            "enabled_strategies": ["iv_rank", "TOTALLY_FAKE", "pead"],
        },
    })
    assert "not_a_real_field" not in saved["overrides"]
    assert saved["overrides"]["risk_per_trade_pct"] == 0.04
    assert saved["overrides"]["enabled_strategies"] == ["iv_rank", "pead"]


def test_invalid_tier_falls_back_to_auto():
    saved = uc.save_user_config({"tier": "hacker", "overrides": {}})
    assert saved["tier"] == "auto"


def test_position_caps_coerced_and_clamped():
    uc.save_user_config({"tier": "auto",
                         "overrides": {"max_swing_positions": "6", "max_day_positions": 999}})
    cfg = uc.effective_config(100_000)
    assert cfg["max_swing_positions"] == 6     # string coerced to int
    assert cfg["max_day_positions"] == 20      # clamped to the bound


# ── Pin + override behaviour ─────────────────────────────────────────────────

def test_pinned_tier_overrides_auto():
    uc.save_user_config({"tier": "standard", "overrides": {}})
    cfg = uc.effective_config(1_000)           # tiny account...
    assert cfg["tier"] == "standard"           # ...but standard is pinned
    assert cfg["tier_source"] == "pinned"
    assert cfg["auto_tier"] == "micro"         # still reports what auto would pick


def test_override_merges_on_top_of_tier():
    uc.save_user_config({"tier": "small", "overrides": {"max_swing_positions": 6}})
    cfg = uc.effective_config(8_000)
    assert cfg["tier"] == "small"
    assert cfg["max_swing_positions"] == 6                     # overridden
    assert cfg["enabled_strategies"] == uc.TIER_PRESETS["small"]["enabled_strategies"]  # tier default


def test_save_is_picked_up_without_manual_cache_clear():
    assert uc.effective_config(100_000)["risk_per_trade_pct"] == 0.03
    uc.save_user_config({"tier": "auto", "overrides": {"risk_per_trade_pct": 0.06}})
    assert uc.effective_config(100_000)["risk_per_trade_pct"] == 0.06   # cache invalidated on save


# ── risk_manager integration ─────────────────────────────────────────────────

def test_get_position_size_is_tiered():
    # small tier risks 5%, standard 3% — sizing must follow the account.
    assert rm.get_position_size(8_000) == pytest.approx(8_000 * 0.05)
    assert rm.get_position_size(100_000) == pytest.approx(100_000 * 0.03)


def _stub_checks(monkeypatch, equity, positions=0):
    monkeypatch.setattr(rm, "get_account_balance", lambda: {"total_equity": equity})
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda eq: (False, 0.0))
    monkeypatch.setattr(rm, "is_already_in_position", lambda t: False)
    monkeypatch.setattr(rm, "concentration_reject", lambda t: None)
    monkeypatch.setattr(rm, "_strategy_budget", lambda strat, eq, b: (b, None))
    monkeypatch.setattr(rm, "count_positions_by_type", lambda tt: positions)
    monkeypatch.setattr(mr, "is_bear_market", lambda: False)


def test_pre_trade_check_rejects_disabled_strategy(monkeypatch):
    # bounce is enabled at standard/small but disabled on a micro account (<$5k).
    # (Uses bounce rather than hft, which is now globally PAUSED and blocked
    # earlier — this keeps the test focused on the per-TIER enable mechanism.)
    _stub_checks(monkeypatch, equity=3_000)
    res = rm.pre_trade_check("AAPL", strategy="bounce")
    assert not res["approved"]
    assert "disabled" in res["reason"]
    assert "micro" in res["reason"]


def test_pre_trade_check_allows_enabled_strategy(monkeypatch):
    _stub_checks(monkeypatch, equity=8_000)
    res = rm.pre_trade_check("AAPL", strategy="iv_rank")   # enabled in small tier
    assert res["approved"]


def test_pre_trade_check_swing_cap_follows_tier(monkeypatch):
    # small tier caps swing at 4 → a 4-position book is full.
    _stub_checks(monkeypatch, equity=8_000, positions=4)
    res = rm.pre_trade_check("AAPL", strategy="iv_rank")
    assert not res["approved"]
    assert "4/4" in res["reason"]


def test_pre_trade_check_standard_account_unchanged(monkeypatch):
    # The whole point: a big account behaves exactly as before (8 swing slots).
    _stub_checks(monkeypatch, equity=100_000, positions=4)
    res = rm.pre_trade_check("AAPL", strategy="pead")
    assert res["approved"]                                 # active strategy, slots free


def test_is_strategy_enabled_helper():
    assert uc.is_strategy_enabled("iv_rank", 100_000) is True       # active swing engine
    assert uc.is_strategy_enabled("hft_intraday", 100_000) is False # PAUSED pending revalidation
    assert uc.is_strategy_enabled("hft_intraday", 8_000) is False   # also PDT-disabled below 25k
    assert uc.is_strategy_enabled(None, 8_000) is True     # no strategy → always ok


# ── min-one-contract sizing ──────────────────────────────────────────────────

def test_min_one_contract_preset_defaults():
    assert uc.TIER_PRESETS["standard"]["min_one_contract"] is False   # large accts unchanged
    assert uc.TIER_PRESETS["small"]["min_one_contract"] is True
    assert uc.TIER_PRESETS["micro"]["min_one_contract"] is True


def test_min_one_contract_in_effective_and_overridable():
    assert uc.effective_config(100_000)["min_one_contract"] is False
    uc.save_user_config({"tier": "auto", "overrides": {"min_one_contract": True}})
    assert uc.effective_config(100_000)["min_one_contract"] is True   # bool coerced + applied


def test_size_contracts_normal_case_unchanged():
    # Budget affords 2 contracts → min-one logic never engages, any account.
    assert rm.size_contracts(1000, 4.25, 100_000) == 2
    assert rm.size_contracts(1000, 4.25, 3_000) == 2


def test_size_contracts_large_account_no_min_one():
    # $100k → standard tier (min_one OFF): a budget too small for 1 contract
    # still returns 0, exactly like the old calculate_contracts.
    assert rm.size_contracts(100, 4.00, 100_000) == 0


def test_size_contracts_small_account_rounds_up_to_one():
    # $8k → small tier (min_one ON), ceiling = 10% * 8000 = $800.
    # Budget $100 buys 0, but one $400 contract fits the ceiling → take 1.
    assert rm.size_contracts(100, 4.00, 8_000) == 1


def test_size_contracts_respects_per_position_ceiling():
    # $3k → micro tier, ceiling = 15% * 3000 = $450. A $500 contract exceeds it,
    # so even with min_one ON we refuse (account too small for THIS contract).
    assert rm.size_contracts(50, 5.00, 3_000) == 0


def test_size_contracts_min_one_can_be_disabled_by_user():
    # Turn the toggle off on a small account → back to strict (0).
    uc.save_user_config({"tier": "small", "overrides": {"min_one_contract": False}})
    assert rm.size_contracts(100, 4.00, 8_000) == 0


# ── liquidity / order-size cap ───────────────────────────────────────────────

def test_liquidity_cap_from_open_interest():
    assert rm.liquidity_cap({"open_interest": 1000, "volume": 0}) == 50    # 5% of 1000


def test_liquidity_cap_uses_volume_when_oi_zero():
    assert rm.liquidity_cap({"open_interest": 0, "volume": 2000}) == 200   # 10% of 2000


def test_liquidity_cap_takes_more_liquid_signal():
    # max(5% of 1000 = 50, 10% of 2000 = 200) = 200
    assert rm.liquidity_cap({"open_interest": 1000, "volume": 2000}) == 200


def test_liquidity_cap_absolute_backstop():
    assert rm.liquidity_cap({"open_interest": 10_000_000, "volume": 0}) == rm.MAX_CONTRACTS_ABS


def test_liquidity_cap_floors_at_one():
    assert rm.liquidity_cap({"open_interest": 5, "volume": 0}) == 1        # 5% of 5 = 0 → 1 lot ok


def test_liquidity_cap_fails_open_without_data():
    assert rm.liquidity_cap({"open_interest": 0, "volume": 0}) is None
    assert rm.liquidity_cap({}) is None
    assert rm.liquidity_cap(None) is None


def test_liquidity_cap_handles_dirty_values():
    assert rm.liquidity_cap({"open_interest": "1000", "volume": None}) == 50   # str/None coerce


def test_size_contracts_clamped_by_liquidity():
    # Standard acct, budget affords 100 contracts, but the option's OI supports
    # only 50 → clamp to 50.
    c = {"open_interest": 1000, "volume": 0}
    assert rm.size_contracts(40_000, 4.00, 100_000, contract=c) == 50


def test_size_contracts_no_cap_without_liquidity_data():
    assert rm.size_contracts(40_000, 4.00, 100_000, contract={}) == 100    # fail open → full size


def test_size_contracts_one_lot_survives_thin_liquidity():
    # Small-acct min-one: a thin option (OI=3) still allows the single lot.
    c = {"open_interest": 3, "volume": 0}
    assert rm.size_contracts(100, 4.00, 8_000, contract=c) == 1


# ── scanner-alert config ─────────────────────────────────────────────────────

def test_alert_config_defaults():
    a = uc.alert_config()
    assert a["enabled"] is True
    assert a["min_score"] == 60
    assert set(a["strategies"]) == set(uc.KNOWN_STRATEGIES)
    assert a["watchlist"] == []


def test_alert_config_clamps_min_score():
    uc.save_user_config({"alerts": {"min_score": 999}})
    assert uc.alert_config()["min_score"] == 100
    uc.save_user_config({"alerts": {"min_score": -5}})
    assert uc.alert_config()["min_score"] == 0


def test_alert_watchlist_cleaned():
    uc.save_user_config({"alerts": {"watchlist": ["nvda", "TSLA", "nvda", "bad!", "toolongticker", " spy "]}})
    assert uc.alert_config()["watchlist"] == ["NVDA", "TSLA", "SPY"]   # upper, dedup, drop invalid


def test_alert_watchlist_accepts_comma_string():
    uc.save_user_config({"alerts": {"watchlist": "nvda, tsla spy"}})
    assert uc.alert_config()["watchlist"] == ["NVDA", "TSLA", "SPY"]


def test_alert_strategies_subset_only():
    uc.save_user_config({"alerts": {"strategies": ["iv_rank", "FAKE", "pead"]}})
    assert uc.alert_config()["strategies"] == ["iv_rank", "pead"]


def test_save_alerts_and_tier_preserve_each_other():
    # Save tier + overrides, then save alerts → tier/overrides must survive.
    uc.save_user_config({"tier": "small", "overrides": {"max_swing_positions": 3}})
    uc.save_user_config({"alerts": {"enabled": False}})
    raw = uc.load_user_config()
    assert raw["tier"] == "small"
    assert raw["overrides"]["max_swing_positions"] == 3
    assert raw["alerts"]["enabled"] is False
    # Save tier again → alerts must survive.
    uc.save_user_config({"tier": "auto", "overrides": {}})
    raw2 = uc.load_user_config()
    assert raw2["tier"] == "auto"
    assert raw2["alerts"]["enabled"] is False
