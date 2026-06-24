"""Tests for the dashboard strategy-panel, events feed, and event logging."""

import json
import os
import time

import dashboard_state as ds
import heartbeat as hb
import risk_manager as rm
import event_notifier as en


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def test_build_strategy_panel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("open_positions.json", {"AAPL_C": {"underlying": "AAPL"}})                 # catalyst 1
    _write("hft_positions.json", [{"underlying": "NVDA"}, {"underlying": "MSFT"}])    # hft 2
    _write("iv_rank_positions.json", [])
    _write("pead_positions.json", [{"underlying": "AMD"}])                            # pead 1
    _write("bounce_positions.json", [])                                               # bounce 0

    monkeypatch.setattr(hb, "read_heartbeats", lambda: {
        "executor":     {"strategy": "executor",     "status": "scanning", "ts": time.time()},
        "hft_executor": {"strategy": "hft_executor", "status": "idle",     "ts": time.time() - 10000},
    })
    monkeypatch.setattr(rm, "deployed_capital_by_strategy",
                        lambda: {"catalyst_long_call": 4000.0, "hft_intraday": 1000.0})
    monkeypatch.setattr(ds, "_market_regime", lambda: {"state": "bull", "detail": "test"})

    closed = [
        {"strategy": "catalyst_long_call", "pnl_dollar": 100},
        {"strategy": "catalyst_long_call", "pnl_dollar": -40},
        {"strategy": "pead", "pnl_dollar": 250},
    ]
    panel = ds.build_strategy_panel(equity=100_000, closed_trades=closed)

    # Five strategies now: catalyst, iv_rank, hft, pead, bounce.
    assert len(panel["health"]) == 5 and len(panel["strategies"]) == 5
    by = {s["key"]: s for s in panel["strategies"]}
    assert by["catalyst_long_call"]["positions"] == 1
    assert by["hft_intraday"]["positions"] == 2
    assert by["pead"]["positions"] == 1
    assert by["bounce"]["positions"] == 0
    assert by["catalyst_long_call"]["usage_pct"] == 10      # 4k of 40k (40% of 100k)
    assert by["catalyst_long_call"]["win_rate"] == 50       # 1 of 2 wins
    assert by["catalyst_long_call"]["pnl"] == 60            # 100 - 40

    h = {x["key"]: x for x in panel["health"]}
    assert h["catalyst_long_call"]["alive"] is True
    assert h["hft_intraday"]["alive"] is False              # 10000s > 600 stale
    assert h["iv_rank"]["status"] == "offline"              # no heartbeat present
    assert h["bounce"]["status"] == "offline"               # bounce strategy not running

    conc = {c["group"]: c["count"] for c in panel["concentration"]}
    assert conc.get("megacap growth") == 3                  # AAPL + NVDA + MSFT
    assert conc.get("semis") == 1                           # AMD
    assert panel["regime"]["state"] == "bull"


def test_build_events_newest_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("events.json", [{"subject": "old"}, {"subject": "new"}])
    ev = ds.build_events()
    assert ev[0]["subject"] == "new"                        # last appended shows first


def test_build_events_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ds.build_events() == []


def test_dispatch_logs_to_events_feed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    en._dispatch("Trade filled — AAPL", ["Qty: 1", "Cost: $500"], "success")
    assert os.path.exists("events.json")
    ev = ds.build_events()
    assert ev[0]["subject"] == "Trade filled — AAPL"
    assert ev[0]["severity"] == "success"
    assert "Qty: 1" in ev[0]["summary"]


def test_compute_pnl_summary():
    # Realized from the merged closed-trade history (local journal + broker fills).
    history = [{"pnl_dollar": 120.50}, {"pnl_dollar": -40}, {"pnl_dollar": 30}]
    # Open positions: grouped (total_pnl_dollar) plus a flat single-leg that only
    # carries pnl_dollar — the fallback must still pick it up.
    positions = [{"total_pnl_dollar": 75.25}, {"total_pnl_dollar": -10}, {"pnl_dollar": 5}]
    s = ds.compute_pnl_summary(history, positions)
    assert s["realized"]   == 110.5    # 120.50 - 40 + 30
    assert s["unrealized"] == 70.25    # 75.25 - 10 + 5
    assert s["total"]      == 180.75   # realized + unrealized
    assert s["total_pct"] is None      # no start_equity supplied


def test_compute_pnl_summary_pct():
    # With a starting-capital baseline, each figure also reports a % return.
    s = ds.compute_pnl_summary([{"pnl_dollar": 100}], [{"total_pnl_dollar": 50}], start_equity=1000)
    assert s["total"] == 150
    assert s["realized_pct"]   == 10.0   # 100 / 1000
    assert s["unrealized_pct"] == 5.0    # 50 / 1000
    assert s["total_pct"]      == 15.0   # 150 / 1000
    assert s["start_equity"]   == 1000


def test_compute_pnl_summary_empty():
    s = ds.compute_pnl_summary([], [])
    assert (s["realized"], s["unrealized"], s["total"]) == (0, 0, 0)
    assert s["total_pct"] is None


def test_compute_pnl_summary_handles_none_and_missing():
    # None or absent P&L fields must be treated as zero, never raise.
    s = ds.compute_pnl_summary([{"pnl_dollar": None}, {}], [{"total_pnl_dollar": None}, {}])
    assert (s["realized"], s["unrealized"], s["total"]) == (0, 0, 0)


def test_tag_positions_with_strategy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Catalyst book: a dict keyed BY the option symbol.
    _write("open_positions.json",
           {"IWM260717C00298000": {"strategy": "catalyst_long_call", "underlying": "IWM"}})
    # IV-rank book: a list of multi-leg records. The per-record "strategy" is the
    # STRUCTURE name ("bull_put_spread") — attribution must use the book's key.
    _write("iv_rank_positions.json", [
        {"strategy": "bull_put_spread", "ticker": "AMAT",
         "legs": [{"symbol": "AMAT260710P00600000"}, {"symbol": "AMAT260710P00550000"}]},
    ])
    _write("hft_positions.json", [])
    _write("pead_positions.json", [])

    # Grouped broker positions as build_positions_data emits them.
    positions = [
        {"legs": [{"symbol": "IWM260717C00298000"}]},
        {"legs": [{"symbol": "AMAT260710P00600000"}, {"symbol": "AMAT260710P00550000"}]},
        {"legs": [{"symbol": "TSLA260101C01000000"}]},   # in no book
    ]
    ds.tag_positions_with_strategy(positions)
    assert positions[0]["strategy"] == "catalyst_long_call"
    assert positions[1]["strategy"] == "iv_rank"          # the BOOK key, not "bull_put_spread"
    assert positions[2]["strategy"] == "unattributed"     # broker-only, no local-book match


def test_unrealized_by_strategy_groups_by_tag():
    # Sums each position's P&L by its attached strategy tag; missing total -> sum legs.
    positions = [
        {"strategy": "catalyst_long_call", "total_pnl_dollar": 120},
        {"strategy": "iv_rank", "legs": [{"pnl_dollar": -30}, {"pnl_dollar": 18}]},
        {"strategy": "unattributed", "total_pnl_dollar": 40},
    ]
    out = ds.unrealized_by_strategy(positions)
    assert out["catalyst_long_call"] == 120
    assert out["iv_rank"] == -12
    assert out["unattributed"] == 40


def test_strategy_panel_carries_unrealized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("open_positions.json",
           {"IWM260717C00298000": {"strategy": "catalyst_long_call", "underlying": "IWM"}})
    monkeypatch.setattr(hb, "read_heartbeats", lambda: {})
    monkeypatch.setattr(rm, "deployed_capital_by_strategy", lambda: {})
    monkeypatch.setattr(ds, "_market_regime", lambda: {"state": "bull", "detail": "t"})
    positions = [{"legs": [{"symbol": "IWM260717C00298000", "pnl_dollar": 95}]}]
    panel = ds.build_strategy_panel(equity=100_000, closed_trades=[], positions=positions)
    by = {s["key"]: s for s in panel["strategies"]}
    assert by["catalyst_long_call"]["unrealized"] == 95
    assert panel["unrealized_unattributed"] == 0.0
