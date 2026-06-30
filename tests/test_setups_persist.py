"""Tests for accumulating scanner-setup persistence + MAX_OPEN_POSITIONS."""

import datetime

import mawitek.dashboard.dashboard_state as ds
import mawitek.core.risk_manager as rm
from mawitek.infra.utils import now_est


def _tickers(setups):
    return sorted(s["ticker"] for s in setups)


def _aged(hours_ago: float) -> str:
    return (now_est() - datetime.timedelta(hours=hours_ago)).isoformat()


def test_fresh_setups_are_saved_and_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    setups = [{"ticker": "NVDA", "setup_score": 80}, {"ticker": "AAPL", "setup_score": 70}]
    out, ts = ds._persist_or_restore_setups(setups)
    assert _tickers(out) == ["AAPL", "NVDA"]
    assert ts is not None
    assert (tmp_path / "setups.json").exists()


def test_empty_cycle_keeps_accumulated_list(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    ds._persist_or_restore_setups([{"ticker": "NVDA", "setup_score": 80}])
    out, ts = ds._persist_or_restore_setups([])      # idle cycle deletes nothing
    assert _tickers(out) == ["NVDA"]
    assert ts is not None


def test_empty_with_no_history_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "none.json"))
    out, ts = ds._persist_or_restore_setups([])
    assert out == []
    assert ts is None


def test_setups_accumulate_across_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 50}])
    out, _ = ds._persist_or_restore_setups([{"ticker": "BBB", "setup_score": 60}])
    # Both present — nothing deleted; sorted by score desc
    assert _tickers(out) == ["AAA", "BBB"]
    assert out[0]["ticker"] == "BBB"  # higher score first


def test_same_ticker_refreshes_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 50}])
    out, _ = ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 80}])
    assert len(out) == 1                 # not duplicated
    assert out[0]["setup_score"] == 80   # newest data wins
    assert "first_seen" in out[0] and "last_seen" in out[0]


def test_first_seen_preserved_on_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    out1, _ = ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 50}])
    first_seen = out1[0]["first_seen"]
    out2, _ = ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 55}])
    assert out2[0]["first_seen"] == first_seen   # original first_seen kept


def test_accumulated_list_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    monkeypatch.setattr(ds, "SCANNER_SETUPS_MAX", 10)
    big = [{"ticker": f"T{i}", "setup_score": i} for i in range(25)]
    out, _ = ds._persist_or_restore_setups(big)
    assert len(out) == 10
    # Highest scores survive the cap
    assert out[0]["setup_score"] == 24


def test_setups_are_not_expired_by_age(tmp_path, monkeypatch):
    # Setups accumulate and are NEVER deleted by a timer — even old ones stay on
    # the board so you can review what the scanner surfaced earlier (incl.
    # after-hours / weekend finds).
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 70, "trade_style": "day"}])
    # Age the saved row far past the old TTLs, then run an idle cycle.
    import json
    p = tmp_path / "setups.json"
    saved = json.loads(p.read_text())
    saved["setups"][0]["last_seen"] = _aged(48)   # 2 days old
    p.write_text(json.dumps(saved))
    out, _ = ds._persist_or_restore_setups([])
    assert _tickers(out) == ["AAA"]               # still on the board, not deleted


def test_found_at_set_and_preserved_on_refresh(tmp_path, monkeypatch):
    # Every setup carries `found_at` (= first_seen) — the date/time it was first
    # found — and it survives later refreshes while the data updates.
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    out1, _ = ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 50}])
    assert out1[0]["found_at"] == out1[0]["first_seen"]
    found = out1[0]["found_at"]
    out2, _ = ds._persist_or_restore_setups([{"ticker": "AAA", "setup_score": 90}])
    assert out2[0]["found_at"] == found            # original found time preserved
    assert out2[0]["setup_score"] == 90            # but the data refreshes


def test_old_and_new_setups_coexist_recent_first(tmp_path, monkeypatch):
    # A historical setup and a fresh one both stay on the board; the most
    # recently-seen one sorts first (live setups on top, history below).
    monkeypatch.setattr(ds, "SCANNER_SETUPS_FILE", str(tmp_path / "setups.json"))
    ds._persist_or_restore_setups([{"ticker": "OLD", "setup_score": 90}])
    import json
    p = tmp_path / "setups.json"
    saved = json.loads(p.read_text())
    saved["setups"][0]["last_seen"] = _aged(30)
    p.write_text(json.dumps(saved))
    out, _ = ds._persist_or_restore_setups([{"ticker": "NEW", "setup_score": 50}])
    assert _tickers(out) == ["NEW", "OLD"]   # both retained
    assert out[0]["ticker"] == "NEW"         # most-recently-seen first


def test_max_open_positions_is_thirteen():
    # 8 swing (catalyst + iv_rank + pead + bounce) + 5 day (hft) = 13
    assert rm.MAX_OPEN_POSITIONS == 13


def test_risk_per_trade_raised():
    assert rm.RISK_PER_TRADE_PCT == 0.03
