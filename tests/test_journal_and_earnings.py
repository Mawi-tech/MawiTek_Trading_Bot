"""Tests for trade journal P&L math, earnings provider cache logic, heartbeat."""

import datetime
import time

import trade_journal as tj
import earnings_provider as ep
import heartbeat as hb


# ── Trade journal P&L ────────────────────────────────────────────────────────

def test_record_closed_trade_pnl_math(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", str(tmp_path / "closed.json"))
    rec = tj.record_closed_trade(
        option_symbol="AAPL260101C00200000",
        underlying="AAPL",
        entry_price=4.00,
        exit_price=6.00,
        quantity=2,
        expiration="2026-01-01",
        entry_time=datetime.datetime.now().isoformat(),
        exit_reason="take_profit",
        strategy="catalyst_long_call",
    )
    # (6 - 4) * 100 * 2 = 400
    assert rec["pnl_dollar"] == 400.0
    # (6-4)/4 * 100 = 50%
    assert rec["pnl_pct"] == 50.0
    assert rec["strategy"] == "catalyst_long_call"


def test_record_closed_trade_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", str(tmp_path / "closed.json"))
    rec = tj.record_closed_trade(
        option_symbol="X260101P00100000", underlying="X",
        entry_price=5.0, exit_price=2.5, quantity=1,
        expiration="2026-01-01", entry_time=None, exit_reason="stop_loss",
    )
    assert rec["pnl_dollar"] == -250.0
    assert rec["pnl_pct"] == -50.0


def test_record_closed_trade_persists_and_appends(tmp_path, monkeypatch):
    f = str(tmp_path / "closed.json")
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", f)
    tj.record_closed_trade("A", "A", 1.0, 2.0, 1, "2026-01-01", None, "tp")
    tj.record_closed_trade("B", "B", 1.0, 0.5, 1, "2026-01-01", None, "sl")
    trades = tj.load_closed_trades()
    assert len(trades) == 2


# ── Earnings provider cache logic ────────────────────────────────────────────

def test_coerce_date_variants():
    assert ep._coerce_date(datetime.date(2026, 7, 31)) == datetime.date(2026, 7, 31)
    assert ep._coerce_date(datetime.datetime(2026, 7, 31, 9, 0)) == datetime.date(2026, 7, 31)
    assert ep._coerce_date("2026-07-31") == datetime.date(2026, 7, 31)
    assert ep._coerce_date("garbage") is None
    assert ep._coerce_date(None) is None


def test_cache_freshness_near_date():
    today = datetime.date(2026, 5, 30)
    fresh = {
        "date": "2026-06-10",  # 11 days out → near (24h TTL)
        "fetched_at": datetime.datetime.now().isoformat(),
    }
    assert ep._cache_is_fresh(fresh, today) is True


def test_cache_staleness_expired():
    today = datetime.date(2026, 5, 30)
    old = {
        "date": "2026-06-10",
        "fetched_at": (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat(),
    }
    # 2 days old vs 24h near-TTL → stale
    assert ep._cache_is_fresh(old, today) is False


def test_cache_missing_fetched_at_is_stale():
    assert ep._cache_is_fresh({"date": "2026-06-10"}, datetime.date(2026, 5, 30)) is False


# ── Heartbeat ────────────────────────────────────────────────────────────────

def test_heartbeat_beat_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "HEARTBEAT_DIR", str(tmp_path / "hb"))
    hb.beat("executor", status="scanning", open_positions=2)
    hbs = hb.read_heartbeats()
    assert "executor" in hbs
    assert hbs["executor"]["status"] == "scanning"
    assert hbs["executor"]["open_positions"] == 2


def test_heartbeat_staleness(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "HEARTBEAT_DIR", str(tmp_path / "hb"))
    hb.beat("hft_executor", status="idle")
    # Fresh → not stale at a large threshold
    assert hb.stale_heartbeats(1000) == []
    # Everything is stale at threshold 0
    stale = hb.stale_heartbeats(0.0)
    assert any(s[0] == "hft_executor" for s in stale)


def test_heartbeat_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "HEARTBEAT_DIR", str(tmp_path / "hb"))
    hb.beat("executor")
    assert "executor" in hb.read_heartbeats()
    hb.clear("executor")
    assert "executor" not in hb.read_heartbeats()
