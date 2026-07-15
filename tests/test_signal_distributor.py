"""
Tests for signal_distributor.py — the multi-target signal fan-out.

Two things must hold no matter what:
  1. SECURITY — only sanitized, account-agnostic signals ever leave the process,
     and the boundary fails CLOSED (a leak raises, it never ships).
  2. CORRECTNESS — dedup means a signal is delivered to a target at most once
     across runs, per-target filters are honoured, and disabled targets are skipped.

No network is touched: the low-level senders are monkeypatched to record calls.
"""

import json

import pytest

import signal_distributor as sd


# ─── Fixtures: isolate the on-disk config + ledger to a temp dir ──────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Run each test in a temp cwd with its own targets + state files."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sd, "TARGETS_FILE", str(tmp_path / "distribution_targets.json"))
    monkeypatch.setattr(sd, "STATE_FILE", str(tmp_path / "distribution_state.json"))
    yield


@pytest.fixture
def sent(monkeypatch):
    """Capture every (target_id, signal_id) actually dispatched, no network."""
    calls: list[tuple[str, str]] = []

    def _fake_send(target, sig):
        calls.append((target["id"], sig.get("id")))
        return True

    monkeypatch.setattr(sd, "_send_to_target", _fake_send)
    return calls


def _write_targets(path, targets):
    with open(path, "w") as f:
        json.dump({"targets": targets}, f)


# A realistic sanitized public signal (what build_public_feed emits).
def _sig(sid, strategy="pead", score=72, conviction="high"):
    return {
        "id": sid, "status": "closed", "ticker": "AAPL", "strategy": strategy,
        "trade_type": "swing", "direction": "bullish", "structure": "long_call",
        "strike": 150.0, "dte": 28, "conviction": conviction, "setup_score": score,
        "rationale": "take_profit", "result_pct": 69.05,
    }


# ─── Security: sanitize boundary ──────────────────────────────────────────────

def test_broadcast_fails_closed_on_account_data(sent):
    """A signal carrying a forbidden field must RAISE, never reach a target."""
    _write_targets(sd.TARGETS_FILE,
                   [{"id": "d1", "type": "discord", "webhook_url": "http://x"}])
    leaky = _sig("sig_1")
    leaky["pnl_dollar"] = 2610.0   # account-private — must trip sanitize()

    with pytest.raises(ValueError):
        sd.broadcast(signals=[leaky])
    assert sent == []              # nothing was dispatched


def test_only_public_fields_are_sent(sent, monkeypatch):
    """The object handed to the sender contains no forbidden substrings."""
    _write_targets(sd.TARGETS_FILE,
                   [{"id": "d1", "type": "discord", "webhook_url": "http://x"}])
    seen = []
    monkeypatch.setattr(sd, "_send_to_target",
                        lambda target, sig: seen.append(sig) or True)
    sd.broadcast(signals=[_sig("sig_1")])
    blob = json.dumps(seen).lower()
    for bad in ("dollar", "equity", "balance", "quantity", "account", "webhook"):
        assert bad not in blob


# ─── Correctness: dedup ───────────────────────────────────────────────────────

def test_signal_sent_once_across_runs(sent):
    _write_targets(sd.TARGETS_FILE,
                   [{"id": "d1", "type": "discord", "webhook_url": "http://x"}])
    sigs = [_sig("sig_1")]

    sd.broadcast(signals=sigs)
    assert sent == [("d1", "sig_1")]

    sd.broadcast(signals=sigs)          # second run: already delivered
    assert sent == [("d1", "sig_1")]    # no new dispatch

    sd.broadcast(signals=sigs + [_sig("sig_2")])   # only the new one goes
    assert sent == [("d1", "sig_1"), ("d1", "sig_2")]


def test_each_target_dedups_independently(sent):
    _write_targets(sd.TARGETS_FILE, [
        {"id": "d1", "type": "discord", "webhook_url": "http://x"},
        {"id": "d2", "type": "discord", "webhook_url": "http://y"},
    ])
    sd.broadcast(signals=[_sig("sig_1")])
    assert sorted(sent) == [("d1", "sig_1"), ("d2", "sig_1")]


# ─── Correctness: filters + enabled flag ──────────────────────────────────────

def test_strategy_filter(sent):
    _write_targets(sd.TARGETS_FILE, [
        {"id": "d1", "type": "discord", "webhook_url": "http://x",
         "strategies": ["iv_rank"]},
    ])
    sd.broadcast(signals=[_sig("sig_1", strategy="pead"),
                          _sig("sig_2", strategy="iv_rank")])
    assert sent == [("d1", "sig_2")]


def test_min_score_and_conviction_filter(sent):
    _write_targets(sd.TARGETS_FILE, [
        {"id": "d1", "type": "discord", "webhook_url": "http://x",
         "min_setup_score": 70, "min_conviction": "high"},
    ])
    sd.broadcast(signals=[
        _sig("low_score", score=50, conviction="high"),
        _sig("low_conv", score=90, conviction="low"),
        _sig("passes", score=90, conviction="high"),
    ])
    assert sent == [("d1", "passes")]


def test_disabled_target_is_skipped(sent):
    _write_targets(sd.TARGETS_FILE, [
        {"id": "d1", "type": "discord", "webhook_url": "http://x", "enabled": False},
    ])
    sd.broadcast(signals=[_sig("sig_1")])
    assert sent == []


# ─── Correctness: dry-run + config validation ─────────────────────────────────

def test_dry_run_sends_and_records_nothing(sent):
    _write_targets(sd.TARGETS_FILE,
                   [{"id": "d1", "type": "discord", "webhook_url": "http://x"}])
    summary = sd.broadcast(signals=[_sig("sig_1")], dry_run=True)
    assert sent == []                       # nothing dispatched
    assert summary["d1"]["sent"] == 1       # but reported as "would send"
    # ledger untouched → a real run afterwards still delivers it
    sd.broadcast(signals=[_sig("sig_1")])
    assert sent == [("d1", "sig_1")]


def test_invalid_targets_are_dropped():
    _write_targets(sd.TARGETS_FILE, [
        {"id": "", "type": "discord", "webhook_url": "http://x"},   # no id
        {"id": "d2", "type": "carrier-pigeon", "webhook_url": "http://x"},  # bad type
        {"id": "d3", "type": "discord"},                            # no webhook
        {"id": "t4", "type": "telegram", "bot_token": "abc"},       # no chat_id
        {"id": "good", "type": "discord", "webhook_url": "http://x"},
    ])
    targets = sd.load_targets()
    assert [t["id"] for t in targets] == ["good"]


def test_no_targets_is_safe_noop(sent):
    # No config file at all.
    summary = sd.broadcast(signals=[_sig("sig_1")])
    assert summary == {}
    assert sent == []
