"""Tests for the VWAP-fade exit configuration: reversion-tuned constants, the
exit_manager preset, and the backtest mirroring the executor."""

import exit_manager
import vwap_fade_executor as ex
import backtest_vwap_fade as bt


def test_exit_constants_are_reversion_tuned():
    assert 0 < ex.STOP_LOSS_PCT < ex.TAKE_PROFIT_PCT          # asymmetric but bounded
    # A fade is faster than the momentum scalper — shorter time-stop.
    import hft_executor as hft
    assert ex.MAX_HOLD_MINUTES < hft.MAX_HOLD_MINUTES
    assert ex.MAX_HOLD_MINUTES == 30
    assert 0 < ex.VWAP_TOUCH_BAND_PCT < ex.STRETCH_STOP_PCT   # touch band tighter than invalidation


def test_vwap_fade_exit_preset_tighter_than_hft():
    fade, hft = exit_manager.VWAP_FADE_EXIT, exit_manager.HFT_EXIT
    # Arm the trail and bank the partial EARLIER than the convex momentum book.
    assert fade.trail_activate < hft.trail_activate
    assert fade.scale_trigger  < hft.scale_trigger
    assert fade.trail_giveback < hft.trail_giveback


def test_backtest_mirrors_executor():
    # The go-live gate must simulate the SAME exits the live executor applies.
    assert bt.TAKE_PROFIT_PCT == ex.TAKE_PROFIT_PCT
    assert bt.STOP_LOSS_PCT == ex.STOP_LOSS_PCT
    assert bt.VWAP_TOUCH_BAND_PCT == ex.VWAP_TOUCH_BAND_PCT
    assert bt.STRETCH_STOP_PCT == ex.STRETCH_STOP_PCT
    assert bt.MAX_HOLD_BARS * 5 == ex.MAX_HOLD_MINUTES        # 6 bars × 5m = 30 min


def test_acceptance_gate_thresholds_present():
    # The two-sample gate must define concrete, non-trivial floors.
    assert bt.GATE_MIN_PF >= 1.0
    assert 0 < bt.GATE_MIN_WIN_RATE <= 100
    assert bt.GATE_MIN_TRADES >= 1
