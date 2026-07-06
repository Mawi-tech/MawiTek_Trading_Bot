"""Tests for SELL/exit alerts + hold-duration reporting (event_notifier)."""

import datetime as dt

import pytest

import event_notifier as en
import user_config as uc


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """Isolated cwd (default alert prefs) + fresh caches."""
    monkeypatch.chdir(tmp_path)
    uc._cache["mtime"] = None
    uc._cache["raw"] = None
    en._SETUP_ALERTED.clear()
    yield


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(en, "_dispatch",
                        lambda subject, lines, severity="info", **kw: sent.append((subject, lines, severity)))
    return sent


# ── _format_held ─────────────────────────────────────────────────────────────

def test_format_held_days():
    now = dt.datetime(2026, 1, 20, 10, 0, 0)
    assert en._format_held("2026-01-17T10:00:00", now=now) == "3d"


def test_format_held_minutes():
    now = dt.datetime(2026, 1, 20, 10, 45, 0)
    assert en._format_held("2026-01-20T10:00:00", now=now) == "45m"


def test_format_held_hours_and_minutes():
    now = dt.datetime(2026, 1, 20, 12, 30, 0)
    assert en._format_held("2026-01-20T10:00:00", now=now) == "2h 30m"


def test_format_held_empty_on_missing_or_bad():
    assert en._format_held(None) == ""
    assert en._format_held("") == ""
    assert en._format_held("not-a-timestamp") == ""


def test_format_held_tz_aware_naive_mismatch_is_safe():
    # tz-aware entry vs naive now would raise on subtraction — must degrade to "".
    aware = "2026-01-20T10:00:00+00:00"
    assert en._format_held(aware, now=dt.datetime(2026, 1, 20, 12, 0, 0)) == ""


# ── hold_horizon ─────────────────────────────────────────────────────────────

def test_hold_horizon_known_strategies():
    assert "16 days" in en.hold_horizon("pead")
    assert "9 days" in en.hold_horizon("bounce")
    assert "intraday" in en.hold_horizon("hft_intraday")


def test_hold_horizon_fallback_from_style():
    assert en.hold_horizon("something_new", style="day") == "intraday"
    assert "swing" in en.hold_horizon("something_new", style="swing")


# ── notify_position_closed → SELL alert with Held line ───────────────────────

def test_close_alert_is_framed_as_sell_with_hold(monkeypatch):
    sent = _capture(monkeypatch)
    entry = (dt.datetime.now() - dt.timedelta(days=4)).isoformat()
    en.notify_position_closed(ticker="NVDA", contract="$120C", pnl_dollar=250.0,
                              pnl_pct=33.3, reason="take_profit", strategy="pead",
                              entry_time=entry)
    assert len(sent) == 1
    subject, lines, severity = sent[0]
    assert subject.startswith("SELL NVDA")
    assert "+33.3%" in subject
    assert any(l.startswith("Held: 4d") for l in lines)
    assert severity == "success"


def test_close_alert_loss_severity_and_no_hold_line(monkeypatch):
    sent = _capture(monkeypatch)
    en.notify_position_closed(ticker="AAPL", contract="$200C", pnl_dollar=-80.0,
                              pnl_pct=-40.0, reason="stop_loss")
    subject, lines, severity = sent[0]
    assert subject.startswith("SELL AAPL")
    assert severity == "danger"
    assert not any(l.startswith("Held:") for l in lines)   # no entry_time → omitted


# ── notify_trade_setups → BUY framing + expected hold ────────────────────────

def test_entry_alert_has_buy_and_expected_hold(monkeypatch):
    sent = _capture(monkeypatch)
    n = en.notify_trade_setups([{"ticker": "TSLA", "setup_score": 80, "direction": "bullish"}],
                               style="swing", strategy="pead")
    assert n == 1
    _, lines, _ = sent[0]
    assert any(l.startswith("BUY TSLA") or l.startswith("[watchlist] BUY TSLA") for l in lines)
    assert any(l.startswith("Expected hold:") and "16 days" in l for l in lines)


# ── trade_plan (buy / sell / time detail) ────────────────────────────────────

def test_trade_plan_pead_has_buy_sell_and_timestop():
    plan = en.trade_plan("pead", {"as_of_close": 120.50})
    assert "buy calls now near $120.50" in plan
    assert "+80% target" in plan
    assert "-35% stop" in plan
    assert "time-stop ~16d (by " in plan


def test_trade_plan_hft_is_intraday():
    plan = en.trade_plan("hft_intraday", {})
    assert "+100% target" in plan and "-20% stop" in plan
    assert "60 min" in plan
    assert "near $" not in plan          # no price field → no price anchor


def test_trade_plan_catalyst_holds_through_event():
    plan = en.trade_plan("catalyst_long_call", {"price": 55.0})
    assert "near $55.00" in plan
    assert "through the catalyst" in plan


def test_trade_plan_unknown_strategy_degrades():
    plan = en.trade_plan("mystery", {})
    assert "buy calls now" in plan and "per strategy" in plan


def test_entry_alert_includes_plan_line(monkeypatch):
    sent = _capture(monkeypatch)
    en.notify_trade_setups([{"ticker": "NVDA", "setup_score": 80, "direction": "bullish",
                             "as_of_close": 200.0}],
                           style="swing", strategy="bounce")
    _, lines, _ = sent[0]
    plan_lines = [l for l in lines if l.strip().startswith("Plan:")]
    assert len(plan_lines) == 1
    assert "near $200.00" in plan_lines[0]
    assert "+60% target" in plan_lines[0] and "time-stop ~9d" in plan_lines[0]
