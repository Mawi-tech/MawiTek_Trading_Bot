"""Guards for the validated HFT day-trading config (Jun 2026).

The strategy was tuned to positive expectancy by (a) requiring >=3 confluence and
(b) using ASYMMETRIC exits — a wide take-profit and a tight stop so convex option
winners outweigh the more-frequent small losers. These tests lock in that intent
so a future edit can't silently revert to the breakeven symmetric config.
"""

import mawitek.strategies.hft_executor as hx
import mawitek.strategies.hft_scanner as hs
import backtests.backtest_hft as bt


def test_exits_are_asymmetric():
    # Reward:risk must stay meaningfully skewed (winners run, losers cut fast).
    assert hx.TAKE_PROFIT_PCT >= 2 * hx.STOP_LOSS_PCT


def test_stop_loss_is_tight():
    assert 0 < hx.STOP_LOSS_PCT <= 0.20


# ── Marketable entry limit (the day-trade "no fill" fix) ────────────────────────

def test_entry_limit_crosses_the_ask():
    # Tight spread: 2.00 / 2.10 (mid 2.05). The limit must be AT/ABOVE the ask so
    # the order is marketable and actually fills.
    lim = hx._marketable_limit(ask=2.10, mid=2.05)
    assert lim >= 2.10
    assert lim == round(2.10 * 1.05, 2)


def test_entry_limit_fills_on_wide_spread():
    # Wide 0-DTE spread 1.00 / 2.00 (mid 1.50). The OLD mid×1.05 = 1.575 sat below
    # the 2.00 ask and never filled — the bug. The new ask-based limit clears it.
    old_mid_based = round(1.50 * 1.05, 2)
    lim = hx._marketable_limit(ask=2.00, mid=1.50)
    assert old_mid_based < 2.00        # the old price would NOT have filled
    assert lim >= 2.00                 # the new one crosses the ask → fills


def test_entry_limit_falls_back_to_mid_without_ask():
    assert hx._marketable_limit(ask=0, mid=1.20) == round(1.20 * 1.05, 2)


def test_confluence_floor_present():
    # Jun 30 2026: floor restored to 3 after a theta-honest, two-sample backtest
    # showed 2 is OVERFIT (positive on mega-caps, negative on a broad basket),
    # while 3 is positive on BOTH. Lock >=3 so a future "frequency push" can't
    # silently revert to the unreliable loose-trigger config.
    assert 3 <= hs.HFT_MIN_CONFLUENCE <= 5


def test_backtest_mirrors_executor_exits():
    # The backtest is only a faithful proxy if its exit rules match the executor.
    assert bt.TAKE_PROFIT_PCT == hx.TAKE_PROFIT_PCT
    assert bt.STOP_LOSS_PCT == hx.STOP_LOSS_PCT
    # Hold time: backtest counts 5-minute bars, executor counts minutes.
    assert bt.MAX_HOLD_BARS * 5 == hx.MAX_HOLD_MINUTES
