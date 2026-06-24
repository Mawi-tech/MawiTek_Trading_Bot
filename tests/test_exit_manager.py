"""Tests for the shared trailing-stop + scale-out exit logic."""

import exit_manager as em

CFG = em.TrailScaleConfig(trail_activate=0.40, trail_giveback=0.25,
                          scale_trigger=0.50, scale_fraction=0.5, scale_min_qty=2)


# ─── update_peak ────────────────────────────────────────────────────────────────

def test_peak_ratchets_up_only():
    pos = {}
    assert em.update_peak(pos, 0.30) == 0.30
    assert em.update_peak(pos, 0.55) == 0.55
    assert em.update_peak(pos, 0.20) == 0.55   # a pullback doesn't lower the peak
    assert pos["peak_pnl_pct"] == 0.55


# ─── trailing_stop_hit ──────────────────────────────────────────────────────────

def test_trailing_not_armed_below_activation():
    # Peak never reached the activation threshold → trail can't fire.
    assert em.trailing_stop_hit(-0.10, 0.30, CFG) is False


def test_trailing_fires_after_giveback_from_peak():
    # Peak +0.60, given back 0.25 → now +0.35 ≤ 0.60-0.25 → fire.
    assert em.trailing_stop_hit(0.35, 0.60, CFG) is True


def test_trailing_holds_while_near_peak():
    # Up +0.55 off a +0.60 peak (only 0.05 giveback) → keep running.
    assert em.trailing_stop_hit(0.55, 0.60, CFG) is False


def test_trailing_ignores_winner_still_climbing():
    assert em.trailing_stop_hit(0.70, 0.70, CFG) is False


# ─── scale_out_quantity ─────────────────────────────────────────────────────────

def test_scale_out_half_at_trigger():
    assert em.scale_out_quantity({"quantity": 10}, 0.50, CFG) == 5


def test_scale_out_keeps_at_least_one_runner():
    # Odd small size: 3 × 0.5 = 1 closed, 2 left running.
    assert em.scale_out_quantity({"quantity": 3}, 0.60, CFG) == 1


def test_no_scale_below_trigger():
    assert em.scale_out_quantity({"quantity": 10}, 0.30, CFG) == 0


def test_no_scale_when_too_few_contracts():
    assert em.scale_out_quantity({"quantity": 1}, 0.90, CFG) == 0


def test_scale_out_only_once():
    assert em.scale_out_quantity({"quantity": 10, "scaled_out": True}, 0.90, CFG) == 0


def test_scale_never_closes_whole_position():
    # Even at a huge fraction, one contract always keeps running.
    cfg = em.TrailScaleConfig(0.4, 0.25, 0.5, scale_fraction=1.0)
    assert em.scale_out_quantity({"quantity": 4}, 0.80, cfg) == 3


# ─── per-strategy configs exist ─────────────────────────────────────────────────

def test_strategy_configs_present():
    for cfg in (em.HFT_EXIT, em.PEAD_EXIT, em.BOUNCE_EXIT):
        assert 0 < cfg.trail_giveback < cfg.trail_activate + 1
        assert 0 < cfg.scale_fraction < 1
        assert cfg.scale_trigger > 0
