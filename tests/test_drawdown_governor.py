"""Tests for the drawdown governor in risk_manager.

Protects profits from a slow multi-day bleed the daily-loss halt misses:
graduated de-risk from the high-water mark, a hard peak-drawdown halt, and a
rolling weekly loss limit. The HWM anchors at current equity on first run.
"""

import json

import mawitek.core.risk_manager as rm


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def _set_hwm(value):
    _write(rm.DRAWDOWN_STATE_FILE, {"hwm": value, "hwm_date": "2020-01-01",
                                    "anchored_at": "2020-01-01"})


# ── high-water mark ──────────────────────────────────────────────────────────

def test_hwm_anchors_at_current_on_first_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rm.update_high_water_mark(86000.0) == 86000.0   # no prior peak → anchor now
    assert json.load(open(rm.DRAWDOWN_STATE_FILE))["hwm"] == 86000.0


def test_hwm_ratchets_up_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rm.update_high_water_mark(86000.0)
    assert rm.update_high_water_mark(90000.0) == 90000.0   # new peak
    assert rm.update_high_water_mark(85000.0) == 90000.0   # dip does NOT lower it
    assert rm.update_high_water_mark(0) == 90000.0         # bad read → unchanged


# ── graduated de-risk ────────────────────────────────────────────────────────

def test_full_size_at_or_above_peak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_hwm(100000.0)
    assert rm.drawdown_governor(100000.0) == (1.0, None)
    assert rm.drawdown_governor(97000.0)  == (1.0, None)   # -3%, above first tier


def test_half_size_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_hwm(100000.0)
    assert rm.drawdown_governor(94000.0)[0] == 0.5         # -6%
    assert rm.drawdown_governor(91000.0)[0] == 0.5         # -9%


def test_quarter_size_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_hwm(100000.0)
    assert rm.drawdown_governor(90000.0)[0] == 0.25        # -10%
    assert rm.drawdown_governor(88000.0)[0] == 0.25        # -12%


def test_hard_halt_at_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_hwm(100000.0)
    mult, reason = rm.drawdown_governor(87000.0)           # -13%
    assert mult == 0.0 and reason and "Drawdown halt" in reason
    assert rm.drawdown_governor(80000.0)[0] == 0.0         # -20%


# ── rolling weekly loss limit ────────────────────────────────────────────────

def test_weekly_loss_limit_halts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Equity == HWM so the peak-drawdown path is silent; only weekly should fire.
    _set_hwm(92000.0)
    # 6 daily closes: 5 trading days ago = 100000, now = 92000 → -8%.
    curve = [{"date": d, "equity": e} for d, e in [
        ("2026-06-01", 100000.0), ("2026-06-02", 99000.0), ("2026-06-03", 97000.0),
        ("2026-06-04", 95000.0),  ("2026-06-05", 94000.0), ("2026-06-08", 92000.0),
    ]]
    _write("equity_curve.json", curve)
    mult, reason = rm.drawdown_governor(92000.0)
    assert mult == 0.0 and reason and "Weekly loss limit" in reason


def test_weekly_ok_when_within_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_hwm(99000.0)
    curve = [{"date": d, "equity": e} for d, e in [
        ("2026-06-01", 100000.0), ("2026-06-02", 99500.0), ("2026-06-03", 99000.0),
        ("2026-06-04", 98500.0),  ("2026-06-05", 98800.0), ("2026-06-08", 99000.0),
    ]]
    _write("equity_curve.json", curve)
    assert rm.drawdown_governor(99000.0) == (1.0, None)    # -1% week, fine


def test_rolling_pnl_none_without_enough_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("equity_curve.json", [{"date": "2026-06-01", "equity": 100000.0}])
    assert rm._rolling_pnl_pct() is None


# ── fail-open ────────────────────────────────────────────────────────────────

def test_disabled_governor_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rm, "DRAWDOWN_GOVERNOR", False)
    _set_hwm(100000.0)
    assert rm.drawdown_governor(80000.0) == (1.0, None)    # would halt if enabled


def test_zero_equity_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rm.drawdown_governor(0) == (1.0, None)
