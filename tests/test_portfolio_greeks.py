"""Tests for portfolio greeks aggregation + the risk-manager vega limit."""

import mawitek.core.portfolio_greeks as pg
import mawitek.core.risk_manager as rm


# ─── aggregate_greeks (pure math) ───────────────────────────────────────────────

def test_long_call_is_long_delta_and_vega():
    legs = [{"delta": 0.5, "gamma": 0.02, "theta": -0.05, "vega": 0.10, "quantity": 2}]
    agg = pg.aggregate_greeks(legs)
    # × quantity(2) × 100
    assert agg["net_delta"] == 100.0
    assert agg["net_vega"] == 20.0
    assert agg["net_theta"] == -10.0      # long option bleeds theta
    assert agg["gross_vega"] == 20.0
    assert agg["leg_count"] == 1


def test_short_leg_flips_sign():
    legs = [{"delta": 0.4, "vega": 0.10, "theta": -0.06, "quantity": -3}]
    agg = pg.aggregate_greeks(legs)
    assert agg["net_delta"] == -120.0
    assert agg["net_vega"] == -30.0       # short premium = short vol
    assert agg["net_theta"] == 18.0       # short option EARNS theta


def test_spread_nets_long_and_short_vega():
    # Bull-put credit spread: short the near put, long the far put → net short vega.
    legs = [
        {"vega": 0.12, "delta": -0.35, "quantity": -1},  # short put
        {"vega": 0.08, "delta": -0.20, "quantity":  1},  # long put
    ]
    agg = pg.aggregate_greeks(legs)
    assert agg["net_vega"] == round((-0.12 + 0.08) * 100, 2)   # -4.0
    assert agg["gross_vega"] == round((0.12 + 0.08) * 100, 2)  # 20.0 (size, not netted)


def test_empty_book_is_all_zero():
    agg = pg.aggregate_greeks([])
    assert agg["net_vega"] == 0.0 and agg["leg_count"] == 0


# ─── cached_net_vega ────────────────────────────────────────────────────────────

def test_cached_net_vega_none_without_priced(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "GREEKS_CACHE_FILE", str(tmp_path / "g.json"))
    assert pg.cached_net_vega() is None                  # no file
    pg.atomic_write_json(pg.GREEKS_CACHE_FILE, {"net_vega": 50.0, "priced": False})
    assert pg.cached_net_vega() is None                  # priced=False → unusable
    pg.atomic_write_json(pg.GREEKS_CACHE_FILE, {"net_vega": 50.0, "priced": True})
    assert pg.cached_net_vega() == 50.0


# ─── risk_manager._vega_reject ──────────────────────────────────────────────────

def _set_vega(monkeypatch, value):
    """Force the cached net vega the risk manager reads."""
    import mawitek.core.portfolio_greeks as portfolio_greeks
    monkeypatch.setattr(portfolio_greeks, "cached_net_vega", lambda: value)


def test_vega_reject_blocks_more_long_vol_when_over_cap(monkeypatch):
    _set_vega(monkeypatch, 2000.0)            # very long vol
    equity = 100_000                          # cap = 1% = $1,000
    # A long-vol (option-buying) strategy is refused...
    assert rm._vega_reject("catalyst_long_call", equity) is not None
    assert rm._vega_reject("pead", equity) is not None
    # ...but the short-vol side is allowed (it de-risks the book).
    assert rm._vega_reject("iv_rank", equity) is None


def test_vega_reject_blocks_more_short_vol_when_over_cap(monkeypatch):
    _set_vega(monkeypatch, -2000.0)           # very short vol
    equity = 100_000
    assert rm._vega_reject("iv_rank", equity) is not None       # no more premium selling
    assert rm._vega_reject("catalyst_long_call", equity) is None  # buying vol is fine


def test_vega_reject_allows_within_cap(monkeypatch):
    _set_vega(monkeypatch, 500.0)             # within the $1,000 cap
    assert rm._vega_reject("catalyst_long_call", 100_000) is None


def test_vega_reject_fails_open_without_data(monkeypatch):
    _set_vega(monkeypatch, None)              # greeks unavailable
    assert rm._vega_reject("catalyst_long_call", 100_000) is None


def test_vega_limit_config_present():
    assert rm.PORTFOLIO_VEGA_LIMIT is True
    assert rm.MAX_PORTFOLIO_VEGA_PCT == 0.01
    assert "iv_rank" in rm.SHORT_VEGA_STRATEGIES
    assert "catalyst_long_call" in rm.LONG_VEGA_STRATEGIES
