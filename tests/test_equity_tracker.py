"""Regression tests for equity_tracker — guards the 2026-06-23 corruption bug.

Root cause: when the Tradier balance read fails it returns all-zeros
(total_equity=0). On this margin account cash also reads 0, so the old
fallback `equity = cash + market_value` recorded only the position cost basis
(~$13k), dropping ~$84k of buying power. That poisoned point became the daily
P&L baseline and made "today" read +$73k.
"""

import json
import os

import mawitek.core.equity_tracker as et


def _write_curve(records):
    with open(et.EQUITY_CURVE_FILE, "w") as f:
        json.dump(records, f)


# ── write side: never persist a failed balance read ──────────────────────────

def test_snapshot_skips_when_total_equity_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Failed broker read: all zeros. Positions still have a cost basis whose
    # quotes are unavailable (marked flat → unrealized 0) — exactly the
    # 2026-06-23 signature.
    monkeypatch.setattr(et, "get_account_balance",
                        lambda: {"total_equity": 0, "cash": 0})
    monkeypatch.setattr(et, "calculate_unrealized_pnl", lambda: (0.0, 12957.0))

    assert et.snapshot_equity() is None
    # Nothing corrupt should have been written.
    assert not os.path.exists(et.EQUITY_CURVE_FILE)


def test_snapshot_persists_on_good_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(et, "get_account_balance",
                        lambda: {"total_equity": 90000.0, "cash": 0})
    monkeypatch.setattr(et, "calculate_unrealized_pnl", lambda: (500.0, 20000.0))
    import mawitek.core.trade_journal as trade_journal
    monkeypatch.setattr(trade_journal, "get_realized_pnl_today", lambda: 100.0)

    snap = et.snapshot_equity()
    assert snap is not None
    assert snap["equity"] == 90000.0          # broker total_equity, not reconstruction
    curve = json.load(open(et.EQUITY_CURVE_FILE))
    assert len(curve) == 1 and curve[0]["equity"] == 90000.0


# ── live equity: fall back to last good snapshot, not the bad reconstruction ──

def test_live_equity_falls_back_to_last_known(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_curve([{"date": "2020-01-01", "equity": 90000.0}])
    monkeypatch.setattr(et, "get_account_balance",
                        lambda: {"total_equity": 0, "cash": 0})
    # If the bug regressed this would return 12957, not 90000.
    monkeypatch.setattr(et, "calculate_unrealized_pnl", lambda: (0.0, 12957.0))

    assert et.get_live_equity() == 90000.0


def test_live_equity_uses_broker_when_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_curve([{"date": "2020-01-01", "equity": 90000.0}])
    monkeypatch.setattr(et, "get_account_balance",
                        lambda: {"total_equity": 86000.0, "cash": 0})
    assert et.get_live_equity() == 86000.0


def test_live_equity_reconstructs_only_with_no_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no curve file at all
    monkeypatch.setattr(et, "get_account_balance",
                        lambda: {"total_equity": 0, "cash": 1000.0})
    monkeypatch.setattr(et, "calculate_unrealized_pnl", lambda: (0.0, 5000.0))
    assert et.get_live_equity() == 6000.0   # cash + market_value, last resort


# ── baseline: ignore non-positive (corrupt) snapshots ────────────────────────

def test_baseline_skips_nonpositive_equity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Prior day: a good 88000 followed by a corrupt 0. Baseline must pick 88000.
    _write_curve([
        {"date": "2020-01-01", "equity": 88000.0},
        {"date": "2020-01-01", "equity": 0.0},
    ])
    assert et.get_baseline_equity_for_today() == 88000.0


def test_baseline_none_when_no_prior_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from mawitek.infra.utils import today_est
    _write_curve([{"date": today_est().isoformat(), "equity": 85000.0}])
    # Only today's snapshots exist → no prior baseline.
    assert et.get_baseline_equity_for_today() is None
