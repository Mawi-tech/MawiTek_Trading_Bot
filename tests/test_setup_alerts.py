"""Tests for the subscriber setup alerts (notify_trade_setups)."""

import json

import pytest

import event_notifier as en
import user_config as uc


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """Isolated cwd (no user_config.json → default alert prefs) + fresh caches."""
    monkeypatch.chdir(tmp_path)
    uc._cache["mtime"] = None
    uc._cache["raw"] = None
    en._SETUP_ALERTED.clear()
    yield


def _setup(ticker, score, **kw):
    return {"ticker": ticker, "setup_score": score, **kw}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(en, "_dispatch",
                        lambda subject, lines, severity="info": sent.append((subject, lines)))
    en._SETUP_ALERTED.clear()
    return sent


def _write_alerts(alerts):
    """Persist alert prefs and bust the user_config cache."""
    with open("user_config.json", "w") as f:
        json.dump({"tier": "auto", "overrides": {}, "alerts": alerts}, f)
    uc._cache["mtime"] = None
    uc._cache["raw"] = None


def test_alerts_high_score_setups(monkeypatch):
    sent = _capture(monkeypatch)
    n = en.notify_trade_setups([_setup("NVDA", 80, style_reason="day: high conviction")],
                               style="day", strategy="hft_intraday")
    assert n == 1 and len(sent) == 1
    subject, lines = sent[0]
    assert "Day-trade" in subject
    assert any("NVDA" in l for l in lines)


def test_low_score_not_alerted(monkeypatch):
    sent = _capture(monkeypatch)
    n = en.notify_trade_setups([_setup("AAA", 40)], style="day", strategy="hft_intraday")
    assert n == 0 and sent == []


def test_same_ticker_alerts_once_per_day(monkeypatch):
    sent = _capture(monkeypatch)
    en.notify_trade_setups([_setup("NVDA", 80)], style="day", strategy="hft_intraday")
    n = en.notify_trade_setups([_setup("NVDA", 85)], style="day", strategy="hft_intraday")
    assert n == 0 and len(sent) == 1          # second cycle deduped


def test_swing_and_day_alert_independently(monkeypatch):
    sent = _capture(monkeypatch)
    en.notify_trade_setups([_setup("NVDA", 80)], style="day", strategy="hft_intraday")
    n = en.notify_trade_setups([_setup("NVDA", 80)], style="swing", strategy="catalyst_long_call")
    assert n == 1 and len(sent) == 2          # same ticker, different style → both alert


def test_batched_with_cap(monkeypatch):
    sent = _capture(monkeypatch)
    setups = [_setup(f"T{i}", 70 + i) for i in range(10)]
    n = en.notify_trade_setups(setups, style="swing", strategy="catalyst_long_call")
    assert n == en.ALERT_SETUP_MAX_PER_MSG    # capped per message
    assert len(sent) == 1                     # ...and batched into ONE message


def test_never_raises(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(en, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    en._SETUP_ALERTED.clear()
    # Valid strategy + score so it reaches the (throwing) dispatch — exercises the
    # exception guard, not the strategy filter.
    assert en.notify_trade_setups([_setup("NVDA", 80)], style="day", strategy="hft_intraday") == 0


# ── config-driven behaviour (dashboard-set prefs) ────────────────────────────

def test_alerts_disabled_sends_nothing(monkeypatch):
    sent = _capture(monkeypatch)
    _write_alerts({"enabled": False})
    n = en.notify_trade_setups([_setup("NVDA", 90)], style="day", strategy="hft_intraday")
    assert n == 0 and sent == []


def test_min_score_comes_from_config(monkeypatch):
    _capture(monkeypatch)
    _write_alerts({"enabled": True, "min_score": 85})
    assert en.notify_trade_setups([_setup("NVDA", 80)], style="day", strategy="hft_intraday") == 0
    en._SETUP_ALERTED.clear()
    assert en.notify_trade_setups([_setup("NVDA", 90)], style="day", strategy="hft_intraday") == 1


def test_per_strategy_filter(monkeypatch):
    _capture(monkeypatch)
    _write_alerts({"enabled": True, "strategies": ["iv_rank"]})    # hft not allowed
    assert en.notify_trade_setups([_setup("NVDA", 90)], style="day", strategy="hft_intraday") == 0


def test_watchlist_bypasses_threshold_and_strategy(monkeypatch):
    sent = _capture(monkeypatch)
    _write_alerts({"enabled": True, "min_score": 95, "strategies": ["iv_rank"], "watchlist": ["NVDA"]})
    # NVDA: below the 95 floor AND from a non-allowed strategy — but watchlisted.
    n = en.notify_trade_setups([_setup("NVDA", 30)], style="day", strategy="hft_intraday")
    assert n == 1
    subject, lines = sent[0]
    assert "watchlist" in subject.lower()
    assert any("[watchlist]" in l for l in lines)


def test_watchlist_off_when_alerts_disabled(monkeypatch):
    sent = _capture(monkeypatch)
    _write_alerts({"enabled": False, "watchlist": ["NVDA"]})
    # Master switch off → even a watchlist name stays silent.
    assert en.notify_trade_setups([_setup("NVDA", 99)], style="day", strategy="hft_intraday") == 0
    assert sent == []
