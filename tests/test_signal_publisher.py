"""Security tests for the public signal feed.

The bot trades the OWNER's real account; subscribers must never see account
data. These tests prove the sanitization boundary holds: no dollars, no sizes,
no equity, no keys ever reach the public feed — and the boundary fails CLOSED.
"""

import json

import pytest

import signal_publisher as sp


# A realistic closed-trade row, full of account-private fields.
_TRADE = {
    "option_symbol": "AAPL260101C00150000",
    "underlying":    "AAPL",
    "strategy":      "pead",
    "trade_type":    "swing",
    "entry_price":   4.20,        # private (per-contract cost)
    "exit_price":    7.10,        # private
    "quantity":      9,           # private (position size)
    "expiration":    "2026-01-01",
    "pnl_dollar":    2610.0,      # private (account dollars)
    "pnl_pct":       69.05,       # PUBLIC (percentage result)
    "exit_reason":   "take_profit",
    "setup_score":   72,
    "signals":       {"direction": "bullish", "option_type": "call",
                      "strike": 150.0, "dte": 28, "conviction": "high"},
}

# Things that must NEVER appear anywhere in the public feed (substrings, lower).
_FORBIDDEN_IN_OUTPUT = [
    "entry_price", "exit_price", "quantity", "pnl_dollar", "equity",
    "balance", "cash", "buying_power", "account", "4.2", "7.1", "2610", "\"9\"",
]


def test_public_signal_drops_all_private_fields():
    pub = sp.to_public_signal(_TRADE)
    for forbidden in ("entry_price", "exit_price", "quantity", "pnl_dollar"):
        assert forbidden not in pub
    # Only allowlisted keys survive.
    assert set(pub).issubset(sp.PUBLIC_FIELDS)


def test_public_signal_keeps_the_recommendation():
    pub = sp.to_public_signal(_TRADE)
    assert pub["ticker"] == "AAPL"
    assert pub["strategy"] == "pead"
    assert pub["direction"] == "bullish"
    assert pub["strike"] == 150.0
    assert pub["structure"] == "long_call"
    assert pub["conviction"] == "high"
    assert pub["result_pct"] == 69.05      # percent result is the product


def test_sanitize_fails_closed_on_injected_private_field():
    # If a dollar field ever sneaks into a "public" record, sanitize must RAISE,
    # not silently pass it through.
    with pytest.raises(ValueError):
        sp.sanitize({"ticker": "AAPL", "pnl_dollar": 1000})
    with pytest.raises(ValueError):
        sp.sanitize({"ticker": "AAPL", "account_id": "ABC123"})
    with pytest.raises(ValueError):
        sp.sanitize({"ticker": "AAPL", "quantity": 5})


def test_track_record_is_percentage_only():
    tr = sp.public_track_record([_TRADE, {"pnl_pct": -20.0}, {"pnl_pct": 50.0}])
    assert tr["trades"] == 3
    assert tr["win_rate_pct"] == round(2 / 3 * 100, 1)
    # No dollar/equity keys in the summary.
    assert sp._has_forbidden_key(tr) is None


def test_full_feed_has_no_account_data_leak(tmp_path, monkeypatch):
    """The end-to-end guarantee: serialize the whole feed and grep it for any
    account data. This is the test that actually protects the owner."""
    monkeypatch.chdir(tmp_path)
    with open("closed_trades.json", "w") as f:
        json.dump([_TRADE, dict(_TRADE, underlying="NVDA", pnl_pct=-15.0)], f)

    feed = sp.write_public_feed()
    blob = json.dumps(feed).lower()

    for forbidden in _FORBIDDEN_IN_OUTPUT:
        assert forbidden.lower() not in blob, f"LEAK: '{forbidden}' in public feed"

    # The structural check also passes, and the feed is non-empty.
    assert sp._has_forbidden_key(feed) is None
    assert len(feed["signals"]) == 2
    # The written file is identical (atomic write round-trips clean).
    assert sp._has_forbidden_key(json.load(open("public_feed.json"))) is None


def test_empty_journal_produces_safe_empty_feed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    feed = sp.build_public_feed()
    assert feed["signals"] == []
    assert feed["track_record"]["trades"] == 0
