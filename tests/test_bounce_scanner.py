"""Regression test: bounce setups must not leak the underlying bearish
PEAD direction into alerts/dashboard (they're long-call bounces, not shorts)."""

import bounce_scanner as bs


def _bearish_pead_setup(**kw):
    return {
        "direction": "bearish",
        "setup_score": 70,
        "conviction": "high",
        "event_move": -8.2,
        "days_since": 2,
        **kw,
    }


def test_scan_ticker_reframes_direction_as_bullish(monkeypatch):
    monkeypatch.setattr(bs.ps, "scan_ticker", lambda ticker, bearish_allowed=False: _bearish_pead_setup())

    setup = bs.scan_ticker("AAPL")

    assert setup is not None
    assert setup["direction"] == "bullish"
    assert setup["trade_direction"] == "bullish"
    assert "bearish" not in setup["style_reason"]


def test_scan_ticker_none_when_underlying_not_bearish(monkeypatch):
    monkeypatch.setattr(bs.ps, "scan_ticker",
                        lambda ticker, bearish_allowed=False: _bearish_pead_setup(direction="bullish"))

    assert bs.scan_ticker("AAPL") is None
