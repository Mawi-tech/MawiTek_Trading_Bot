"""Tests for the correlation/concentration cap in risk_manager."""

import json

import mawitek.core.risk_manager as rm


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ── group lookup ─────────────────────────────────────────────────────────────

def test_correlation_group_lookup():
    assert rm.correlation_group("AAPL") == "megacap_growth"
    assert rm.correlation_group("nvda") == "megacap_growth"   # case-insensitive
    assert rm.correlation_group("SPY") == "index"
    assert rm.correlation_group("AMD") == "semis"
    assert rm.correlation_group("XOM") == "energy"


def test_correlation_group_unknown_is_none():
    assert rm.correlation_group("SOME_RANDOM_TICKER") is None
    assert rm.correlation_group(None) is None


def test_first_group_wins_no_double_classification():
    # NVDA is a chipmaker but lives in megacap_growth (listed first) — a ticker
    # must resolve to exactly one cluster.
    assert rm.correlation_group("NVDA") == "megacap_growth"
    assert "NVDA" not in rm.CORRELATION_GROUPS["semis"]


# ── concentration_reject ─────────────────────────────────────────────────────

def test_no_reject_when_cluster_has_room(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("hft_positions.json", [{"underlying": "AAPL"}, {"underlying": "MSFT"}])
    # 2 megacap_growth open; a 3rd is still allowed (cap is 3).
    assert rm.concentration_reject("NVDA") is None


def test_reject_when_cluster_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("hft_positions.json",
           [{"underlying": "AAPL"}, {"underlying": "MSFT"}, {"underlying": "NVDA"}])
    # 3 megacap_growth already open → a 4th correlated name is blocked.
    reason = rm.concentration_reject("AMZN")
    assert reason is not None and "megacap_growth" in reason


def test_other_cluster_unaffected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("hft_positions.json",
           [{"underlying": "AAPL"}, {"underlying": "MSFT"}, {"underlying": "NVDA"}])
    # Energy has room even though megacap_growth is full.
    assert rm.concentration_reject("XOM") is None


def test_ungrouped_ticker_never_capped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("hft_positions.json",
           [{"underlying": "ZZZA"}, {"underlying": "ZZZB"}, {"underlying": "ZZZC"}])
    assert rm.concentration_reject("ZZZD") is None   # none are in a cluster


def test_counts_across_all_three_books(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("open_positions.json", {"AAPL260101C": {"underlying": "AAPL"}})   # catalyst
    _write("hft_positions.json", [{"underlying": "MSFT"}])                    # hft
    _write("iv_rank_positions.json", [{"ticker": "NVDA"}])                    # iv_rank ('ticker' key)
    # 1 from each book = 3 megacap_growth across the whole portfolio → full.
    assert rm.concentration_reject("META") is not None


def test_iv_rank_ticker_field_recognized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("iv_rank_positions.json", [{"ticker": "JPM"}, {"ticker": "BAC"}, {"ticker": "GS"}])
    assert rm.concentration_reject("WFC") is not None    # financials cluster full
