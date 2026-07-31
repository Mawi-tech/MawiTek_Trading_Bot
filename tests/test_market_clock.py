"""Tests for the broker market-clock (tradier_client.get_market_clock /
market_open_now) and the session-aware market-hours guard
(utils.is_market_session_open) that makes the bot holiday/half-day aware."""

import datetime
from zoneinfo import ZoneInfo

import pytest

import tradier_client as tc
import utils

ET = ZoneInfo("America/New_York")

# 2026-07-03: a Friday, but the market was closed (July 4th observed).
HOLIDAY_NOON = datetime.datetime(2026, 7, 3, 12, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _fresh_clock_cache():
    tc._clock_cache.update({"ts": 0.0, "clock": None})
    yield
    tc._clock_cache.update({"ts": 0.0, "clock": None})


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _wire_clock(monkeypatch, state, counter=None):
    monkeypatch.setattr(tc, "MOCK_MODE", False)

    def fake_get(url, headers=None, timeout=None):
        if counter is not None:
            counter["n"] += 1
        return _FakeResp({"clock": {"state": state, "date": "2026-07-03"}})

    monkeypatch.setattr(tc.requests, "get", fake_get)


# ── get_market_clock / market_open_now ───────────────────────────────────────

def test_mock_mode_returns_unknown(monkeypatch):
    # Assert the MOCK_MODE branch explicitly instead of relying on the
    # ambient default. conftest.py now pins MOCK_MODE on for the whole
    # suite, but a test named for this branch should force it, so it stays
    # meaningful if that ever changes.
    monkeypatch.setattr(tc, "MOCK_MODE", True)
    assert tc.get_market_clock() == {}
    assert tc.market_open_now() is None


def test_market_open_now_parses_states(monkeypatch):
    for state, expected in (("open", True), ("closed", False),
                            ("premarket", False), ("postmarket", False)):
        tc._clock_cache.update({"ts": 0.0, "clock": None})
        _wire_clock(monkeypatch, state)
        assert tc.market_open_now() is expected, state


def test_clock_is_ttl_cached(monkeypatch):
    calls = {"n": 0}
    _wire_clock(monkeypatch, "open", calls)
    assert tc.market_open_now() is True
    assert tc.market_open_now() is True
    assert calls["n"] == 1                      # second read served from cache


def test_clock_error_returns_unknown_and_does_not_cache(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)

    def boom(url, headers=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(tc.requests, "get", boom)
    assert tc.get_market_clock() == {}
    assert tc.market_open_now() is None
    assert tc._clock_cache["clock"] is None     # failure never poisons the cache


# ── utils.is_market_session_open ─────────────────────────────────────────────

def test_holiday_closes_the_session_guard(monkeypatch):
    # Friday noon, inside every strategy's window — but the exchange is closed.
    monkeypatch.setattr(tc, "market_open_now", lambda: False)
    assert utils.is_market_open(9, 35, 15, 55, HOLIDAY_NOON)            # blind
    assert not utils.is_market_session_open(9, 35, 15, 55, HOLIDAY_NOON)  # aware


def test_normal_day_stays_open(monkeypatch):
    monkeypatch.setattr(tc, "market_open_now", lambda: True)
    assert utils.is_market_session_open(9, 35, 15, 55, HOLIDAY_NOON)


def test_fails_open_when_clock_unknown(monkeypatch):
    # MOCK_MODE / clock unreadable → behave exactly like is_market_open.
    monkeypatch.setattr(tc, "market_open_now", lambda: None)
    assert utils.is_market_session_open(9, 35, 15, 55, HOLIDAY_NOON)


def test_fails_open_when_clock_raises(monkeypatch):
    def boom():
        raise RuntimeError("broker unreachable")
    monkeypatch.setattr(tc, "market_open_now", boom)
    assert utils.is_market_session_open(9, 35, 15, 55, HOLIDAY_NOON)


def test_weekend_and_window_still_gate_locally(monkeypatch):
    # The broker clock is only consulted INSIDE the local window: a weekend or
    # an out-of-window time is closed regardless of what the clock says.
    monkeypatch.setattr(tc, "market_open_now", lambda: True)
    saturday = datetime.datetime(2026, 7, 4, 12, 0, tzinfo=ET)
    evening  = HOLIDAY_NOON.replace(hour=19)
    assert not utils.is_market_session_open(9, 35, 15, 55, saturday)
    assert not utils.is_market_session_open(9, 35, 15, 55, evening)


def test_executor_guards_use_session_check(monkeypatch):
    # All five strategy guards must consult the broker clock — a holiday
    # closes every one of them.
    pytest.importorskip("ta")   # executor → momentum_scorer → ta (optional dep)
    import executor, pead_executor, bounce_executor, hft_executor

    monkeypatch.setattr(tc, "market_open_now", lambda: False)
    for mod, fn in ((executor, "is_market_hours"),
                    (pead_executor, "_is_market_open"),
                    (bounce_executor, "_is_market_open"),
                    (hft_executor, "_is_market_open")):
        monkeypatch.setattr(mod, "now_est", lambda: HOLIDAY_NOON, raising=False)
        monkeypatch.setattr(utils, "now_est", lambda: HOLIDAY_NOON)
        assert getattr(mod, fn)() is False, f"{mod.__name__}.{fn} ignored the holiday"
