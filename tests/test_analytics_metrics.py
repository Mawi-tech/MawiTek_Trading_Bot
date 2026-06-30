"""Tests for analytics_metrics: Sharpe, drawdown, profit factor, expectancy."""

import math
import mawitek.analysis.analytics_metrics as am


def test_max_drawdown_simple():
    curve = [{"equity": 100}, {"equity": 120}, {"equity": 90}, {"equity": 110}]
    dd = am.max_drawdown(curve)
    # peak 120 → trough 90 = 25% drawdown
    assert dd["pct"] == 25.0
    assert dd["peak"] == 120
    assert dd["trough"] == 90


def test_max_drawdown_monotonic_up_is_zero():
    curve = [{"equity": 100}, {"equity": 110}, {"equity": 120}]
    assert am.max_drawdown(curve)["pct"] == 0.0


def test_total_return():
    curve = [
        {"date": "2026-01-01", "equity": 100},
        {"date": "2026-01-02", "equity": 150},
    ]
    assert am.total_return(curve) == 50.0


def test_total_return_none_with_one_point():
    assert am.total_return([{"date": "2026-01-01", "equity": 100}]) is None


def test_sharpe_none_with_insufficient_data():
    assert am.sharpe_ratio([{"date": "2026-01-01", "equity": 100}]) is None


def test_sharpe_positive_for_steady_gains():
    # Steadily rising equity → positive Sharpe
    curve = [{"date": f"2026-01-{i:02d}", "equity": 100 + i} for i in range(1, 11)]
    s = am.sharpe_ratio(curve)
    assert s is not None and s > 0


def test_trade_metrics_basic():
    trades = [
        {"pnl_dollar": 100, "strategy": "a"},
        {"pnl_dollar": -50, "strategy": "a"},
        {"pnl_dollar": 200, "strategy": "b"},
    ]
    m = am.trade_metrics(trades)
    assert m["count"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1
    assert m["win_rate"] == round(2 / 3 * 100, 1)
    # gross profit 300, gross loss 50 → PF 6.0
    assert m["profit_factor"] == 6.0
    # expectancy (100-50+200)/3
    assert m["expectancy"] == round(250 / 3, 2)


def test_trade_metrics_no_losses_profit_factor_is_json_safe_none():
    # No losers → profit factor is undefined. Must be None, NOT float('inf'),
    # because inf serializes to invalid JSON and breaks the dashboard.
    import json
    trades = [{"pnl_dollar": 10}, {"pnl_dollar": 20}]
    m = am.trade_metrics(trades)
    assert m["profit_factor"] is None
    # The whole metrics block must be strictly JSON-serializable.
    json.dumps(am.compute_metrics([{"date": "2026-01-01", "equity": 100}], trades),
               allow_nan=False)


def test_trade_metrics_empty():
    m = am.trade_metrics([])
    assert m["count"] == 0
    assert m["profit_factor"] is None


def test_per_strategy_split():
    trades = [
        {"pnl_dollar": 100, "strategy": "catalyst_long_call"},
        {"pnl_dollar": -50, "strategy": "hft_intraday"},
    ]
    by = am.per_strategy_metrics(trades)
    assert set(by.keys()) == {"catalyst_long_call", "hft_intraday"}
    assert by["catalyst_long_call"]["wins"] == 1
    assert by["hft_intraday"]["losses"] == 1


def test_compute_metrics_shape():
    curve = [{"date": "2026-01-01", "equity": 100}, {"date": "2026-01-02", "equity": 110}]
    trades = [{"pnl_dollar": 50, "strategy": "a"}]
    m = am.compute_metrics(curve, trades)
    assert "sharpe" in m and "max_drawdown" in m and "total_return_pct" in m
    assert "trades" in m and "by_strategy" in m
