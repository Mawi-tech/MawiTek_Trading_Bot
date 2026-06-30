"""Tests for the scan-loop efficiency caches.

Two caches cut redundant per-cycle API calls in the live scan loop:
  1. market_filter daily-liquidity cache (shared, keyed by ET trading day)
  2. hft_scanner intraday-bar TTL cache (per process, keyed by ticker+interval)
"""

import datetime

import pandas as pd

import mawitek.data.market_filter as mf
import mawitek.strategies.hft_scanner as hs


# ── Daily liquidity cache ────────────────────────────────────────────────────

def _liquid_daily_df():
    """A 25-row daily frame that clears the default liquidity thresholds."""
    return pd.DataFrame({
        "Close":  [100.0] * 25,
        "Volume": [2_000_000] * 25,   # 100 * 2M = $200M dollar volume
    })


def test_liquidity_cache_avoids_refetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_get_daily_data(ticker, period="3mo"):
        calls["n"] += 1
        return _liquid_daily_df()

    monkeypatch.setattr(mf, "get_daily_data", fake_get_daily_data)

    first  = mf.filter_universe(["AAA", "BBB"])
    after_first = calls["n"]
    second = mf.filter_universe(["AAA", "BBB"])

    assert first == ["AAA", "BBB"] == second
    assert after_first == 2          # one fetch per symbol on the cold call
    assert calls["n"] == 2           # second call served entirely from cache


def test_liquidity_cache_can_be_bypassed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_get_daily_data(ticker, period="3mo"):
        calls["n"] += 1
        return _liquid_daily_df()

    monkeypatch.setattr(mf, "get_daily_data", fake_get_daily_data)

    mf.filter_universe(["AAA"], use_cache=False)
    mf.filter_universe(["AAA"], use_cache=False)
    assert calls["n"] == 2           # no caching → fetched both times


def test_liquidity_cache_invalidated_next_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_get_daily_data(ticker, period="3mo"):
        calls["n"] += 1
        return _liquid_daily_df()

    monkeypatch.setattr(mf, "get_daily_data", fake_get_daily_data)

    # Seed a cache file stamped with YESTERDAY's date — it must be ignored.
    from mawitek.infra.state_io import atomic_write_json
    yesterday = (mf.today_est() - datetime.timedelta(days=1)).isoformat()
    atomic_write_json(mf._LIQUIDITY_CACHE_FILE, {
        "date": yesterday,
        "metrics": {"AAA": {"last_close": 100.0,
                            "avg_volume_20": 2_000_000,
                            "avg_dollar_volume_20": 200_000_000}},
    })

    mf.filter_universe(["AAA"])
    assert calls["n"] == 1           # stale cache → re-fetched


# ── Intraday bar TTL cache ───────────────────────────────────────────────────

def _intraday_df():
    idx = pd.date_range("2026-06-03 10:00", periods=20, freq="5min")
    return pd.DataFrame({
        "Open": [100.0] * 20, "High": [101.0] * 20, "Low": [99.0] * 20,
        "Close": [100.5] * 20, "Volume": [1000] * 20,
    }, index=idx)


def test_intraday_cache_reuses_within_ttl(monkeypatch):
    hs.clear_intraday_cache()
    calls = {"n": 0}

    def fake_bars(ticker, interval="5m", days=1):
        calls["n"] += 1
        return _intraday_df()

    monkeypatch.setattr(hs, "get_intraday_bars", fake_bars)

    clock = {"t": 1000.0}
    monkeypatch.setattr(hs.time, "time", lambda: clock["t"])

    hs.fetch_intraday("AAA", interval="5m")
    clock["t"] += 60                 # one loop cycle later, same 5m bar
    hs.fetch_intraday("AAA", interval="5m")
    assert calls["n"] == 1           # second served from cache


def test_intraday_cache_refetches_after_ttl(monkeypatch):
    hs.clear_intraday_cache()
    calls = {"n": 0}

    def fake_bars(ticker, interval="5m", days=1):
        calls["n"] += 1
        return _intraday_df()

    monkeypatch.setattr(hs, "get_intraday_bars", fake_bars)

    clock = {"t": 1000.0}
    monkeypatch.setattr(hs.time, "time", lambda: clock["t"])

    hs.fetch_intraday("AAA", interval="5m")
    clock["t"] += 300                # past the 0.8 * 300 = 240s TTL
    hs.fetch_intraday("AAA", interval="5m")
    assert calls["n"] == 2           # cache expired → re-fetched


def test_intraday_cache_skips_empty(monkeypatch):
    hs.clear_intraday_cache()
    calls = {"n": 0}

    def fake_empty(ticker, interval="5m", days=1):
        calls["n"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(hs, "get_intraday_bars", fake_empty)
    monkeypatch.setattr(hs.time, "time", lambda: 1000.0)

    hs.fetch_intraday("AAA", interval="5m")
    hs.fetch_intraday("AAA", interval="5m")
    assert calls["n"] == 2           # empties are never cached → both fetched
