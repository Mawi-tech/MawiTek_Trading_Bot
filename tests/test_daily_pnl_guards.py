"""Tests for the daily-P&L guards in risk_manager.calculate_daily_pnl():
pre-open/weekend readings and stale baselines must fall back to realized-only
P&L instead of presenting phantom mark-to-market moves as today's loss."""

import datetime
from zoneinfo import ZoneInfo

import pytest

import risk_manager as rm
import equity_tracker as et
import trade_journal as tj

ET = ZoneInfo("America/New_York")


def _wire(monkeypatch, *, now, baseline_date="2026-06-30", baseline_equity=100_000.0,
          live_equity=98_000.0, realized=-50.0, snapshot=...):
    """Stub the clock, the equity snapshots, and the journal around
    calculate_daily_pnl. `now` is an ET datetime; today follows from it."""
    monkeypatch.setattr(rm, "now_est", lambda: now)
    monkeypatch.setattr(rm, "today_est", lambda: now.date())
    if snapshot is ...:
        snapshot = {"date": baseline_date, "equity": baseline_equity}
    monkeypatch.setattr(et, "get_baseline_snapshot_for_today", lambda: snapshot)
    monkeypatch.setattr(tj, "get_realized_pnl_today", lambda: realized)

    calls = {"live": 0}

    def _live():
        calls["live"] += 1
        return live_equity

    monkeypatch.setattr(et, "get_live_equity", _live)
    return calls


# 2026-07-01 is a Wednesday; 2026-07-04 a Saturday; 2026-07-06 a Monday.
WED = datetime.datetime(2026, 7, 1, 11, 0, tzinfo=ET)


def test_market_hours_fresh_baseline_uses_mark_to_market(monkeypatch):
    calls = _wire(monkeypatch, now=WED)   # baseline Tuesday = previous trading day
    assert rm.calculate_daily_pnl() == 98_000.0 - 100_000.0
    assert calls["live"] == 1


def test_pre_open_uses_realized_only(monkeypatch):
    # 08:59 ET: option marks are stale/bid-skewed — never mark-to-market.
    calls = _wire(monkeypatch, now=WED.replace(hour=8, minute=59))
    assert rm.calculate_daily_pnl() == -50.0
    assert calls["live"] == 0   # must not even read the broker equity


def test_open_boundary_is_mark_to_market(monkeypatch):
    calls = _wire(monkeypatch, now=WED.replace(hour=9, minute=30))
    assert rm.calculate_daily_pnl() == -2_000.0
    assert calls["live"] == 1


def test_weekend_uses_realized_only(monkeypatch):
    saturday = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=ET)
    calls = _wire(monkeypatch, now=saturday)
    assert rm.calculate_daily_pnl() == -50.0
    assert calls["live"] == 0


def test_stale_baseline_uses_realized_only(monkeypatch):
    # Last snapshot a week old → equity − baseline would be a multi-day move.
    calls = _wire(monkeypatch, now=WED, baseline_date="2026-06-23")
    assert rm.calculate_daily_pnl() == -50.0
    assert calls["live"] == 0


def test_monday_accepts_friday_baseline(monkeypatch):
    # Friday IS the previous trading day of a Monday — not stale.
    monday = datetime.datetime(2026, 7, 6, 11, 0, tzinfo=ET)
    _wire(monkeypatch, now=monday, baseline_date="2026-07-03")
    assert rm.calculate_daily_pnl() == -2_000.0


def test_no_snapshot_uses_realized_only(monkeypatch):
    _wire(monkeypatch, now=WED, snapshot=None)
    assert rm.calculate_daily_pnl() == -50.0


def test_bad_snapshot_date_uses_realized_only(monkeypatch):
    _wire(monkeypatch, now=WED, snapshot={"date": "not-a-date", "equity": 100_000.0})
    assert rm.calculate_daily_pnl() == -50.0


def test_previous_trading_day():
    assert rm._previous_trading_day(datetime.date(2026, 7, 1)) == datetime.date(2026, 6, 30)  # Wed → Tue
    assert rm._previous_trading_day(datetime.date(2026, 7, 6)) == datetime.date(2026, 7, 3)   # Mon → Fri
    assert rm._previous_trading_day(datetime.date(2026, 7, 5)) == datetime.date(2026, 7, 3)   # Sun → Fri


def test_pre_open_phantom_cannot_latch_the_halt(monkeypatch, tmp_path):
    # The original failure mode end-to-end: pre-open, broker equity reads 35%
    # below yesterday's snapshot (phantom marks), nothing realized today.
    # The daily-loss check must NOT halt.
    monkeypatch.chdir(tmp_path)   # isolate risk_state.json
    _wire(monkeypatch, now=WED.replace(hour=8, minute=0),
          live_equity=65_000.0, realized=0.0)
    halted, pnl = rm.check_daily_loss_limit(100_000.0)
    assert not halted
    assert pnl == 0.0
