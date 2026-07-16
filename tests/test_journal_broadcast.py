"""
Tests for the automatic signal broadcast hook in trade_journal.

Recording a closed trade should fan the sanitized signal out to configured
distribution targets — but ONLY when a signal service is configured, and NEVER
in a way that can break the trade-close path.
"""

import json

import pytest

import trade_journal as tj
import signal_distributor as sd


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", str(tmp_path / "closed_trades.json"))
    monkeypatch.setattr(sd, "TARGETS_FILE", str(tmp_path / "distribution_targets.json"))
    monkeypatch.setattr(sd, "STATE_FILE", str(tmp_path / "distribution_state.json"))
    # signal_publisher reads the journal by its own constant — point it at ours.
    import signal_publisher
    monkeypatch.setattr(signal_publisher, "PUBLIC_FEED_FILE", str(tmp_path / "public_feed.json"))
    yield


def _record_a_close():
    return tj.record_closed_trade(
        option_symbol="AAPL260101C00150000", underlying="AAPL",
        entry_price=4.20, exit_price=7.10, quantity=9, expiration="2026-01-01",
        entry_time=None, exit_reason="take_profit", setup_score=72,
        signals={"direction": "bullish", "option_type": "call",
                 "strike": 150.0, "dte": 28, "conviction": "high"},
        strategy="pead",
    )


def test_no_targets_skips_broadcast(monkeypatch):
    """With no distribution config, the broadcast path must not be entered."""
    called = []
    monkeypatch.setattr(sd, "broadcast", lambda *a, **k: called.append(True))
    t = tj._broadcast_signals_async()
    if t:
        t.join(timeout=5)
    assert called == []                       # load_targets() empty → no broadcast


def test_close_broadcasts_to_configured_target(monkeypatch, tmp_path):
    """A recorded close is delivered (sanitized) to a configured target."""
    with open(sd.TARGETS_FILE, "w") as f:
        json.dump({"targets": [{"id": "d1", "type": "discord",
                                "webhook_url": "http://x"}]}, f)
    sent = []
    monkeypatch.setattr(sd, "_send_to_target",
                        lambda target, sig: sent.append((target["id"], sig)) or True)

    _record_a_close()
    # join the daemon broadcast thread so the assertion is deterministic
    for thr in __import__("threading").enumerate():
        if thr.name == "signal-broadcast":
            thr.join(timeout=5)

    assert len(sent) == 1
    tid, sig = sent[0]
    assert tid == "d1"
    assert sig["ticker"] == "AAPL"
    assert sig["result_pct"] == 69.05
    # No account-private data crossed the boundary.
    blob = json.dumps(sig).lower()
    for bad in ("dollar", "entry_price", "exit_price", "quantity", "equity"):
        assert bad not in blob


def test_broadcast_failure_never_breaks_the_close(monkeypatch):
    """If the distributor blows up, record_closed_trade still returns the record."""
    with open(sd.TARGETS_FILE, "w") as f:
        json.dump({"targets": [{"id": "d1", "type": "discord",
                                "webhook_url": "http://x"}]}, f)
    monkeypatch.setattr(sd, "broadcast",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rec = _record_a_close()
    assert rec["underlying"] == "AAPL"        # close succeeded despite the error
    assert tj.load_closed_trades()            # and it was journaled
