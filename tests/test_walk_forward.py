"""Tests for walk_forward: sequential splitting, degradation, divergence."""

import walk_forward as wf


def _t(pnl, day, strat="a"):
    return {"pnl_dollar": pnl, "exit_time": f"2026-01-{day:02d}T12:00:00", "strategy": strat}


def test_split_sequential_even():
    trades = [_t(1, d) for d in range(1, 9)]  # 8 trades
    windows = wf.split_sequential(trades, 4)
    assert len(windows) == 4
    assert all(len(w) == 2 for w in windows)


def test_split_sequential_preserves_time_order():
    trades = [_t(1, 5), _t(1, 1), _t(1, 3)]
    windows = wf.split_sequential(trades, 3)
    # Oldest first after sort
    assert windows[0][0]["exit_time"].endswith("01T12:00:00")


def test_split_empty():
    assert wf.split_sequential([], 3) == [[], [], []]


def test_walk_forward_shape():
    trades = [_t(10 if d % 2 else -5, d) for d in range(1, 21)]
    rep = wf.evaluate_walk_forward(trades, n_windows=4)
    assert rep["n_windows"] == 4
    assert len(rep["windows"]) == 4
    assert "in_sample" in rep and "out_sample" in rep
    assert "degradation" in rep


def test_degradation_insufficient_data():
    trades = [_t(10, d) for d in range(1, 5)]  # only 4 trades
    rep = wf.evaluate_walk_forward(trades, n_windows=2)
    assert rep["degradation"]["flag"] == "insufficient_data"


def test_degradation_detects_collapse():
    # First half winners, second half losers → out-of-sample collapse
    winners = [_t(100, d) for d in range(1, 11)]
    losers = [_t(-100, d) for d in range(11, 21)]
    rep = wf.evaluate_walk_forward(winners + losers, n_windows=4)
    assert rep["degradation"]["flag"] == "degraded"
    assert rep["out_sample"]["expectancy"] < 0
    assert rep["in_sample"]["expectancy"] > 0


def test_degradation_ok_for_consistent():
    # Consistent ~60% winners throughout → not degraded
    trades = []
    for d in range(1, 41):
        trades.append(_t(50 if d % 5 != 0 else -50, d))
    rep = wf.evaluate_walk_forward(trades, n_windows=4)
    assert rep["degradation"]["flag"] in ("ok", "insufficient_data")


def test_live_vs_backtest_underperforming():
    backtest = [_t(100, d) for d in range(1, 21)]   # backtest: all winners
    live = [_t(-20, d) for d in range(1, 21)]        # live: all losers
    rep = wf.live_vs_backtest(live, backtest)
    assert rep["flag"] == "underperforming"
    assert rep["expectancy_gap"] < 0


def test_live_vs_backtest_insufficient():
    rep = wf.live_vs_backtest([_t(10, 1)], [_t(10, 1)])
    assert rep["flag"] == "insufficient_data"


def test_format_walk_forward_runs():
    trades = [_t(10 if d % 2 else -5, d) for d in range(1, 21)]
    out = wf.format_walk_forward(wf.evaluate_walk_forward(trades, 4))
    assert "Walk-forward analysis" in out
