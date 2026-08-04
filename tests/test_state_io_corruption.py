"""
Corrupt state files must fail closed, not silently vanish.

state_io's own docstring calls out the risk: a corrupt risk_state.json "could
silently disable the daily-loss halt. This is the dangerous one." Atomic writes
closed the path that CREATES corruption; read_json reopened it by catching every
exception and returning `default` — which for risk state means halted=False,
trades_today=0, realized_pnl=0.0. The bot would resume trading into a session it
had already halted, and nothing anywhere would say so.
"""

import glob
import json
import os

import pytest

import state_io
from state_io import StateCorruption


def _corrupt_files(directory):
    return glob.glob(os.path.join(str(directory), "*.corrupt.*"))


# ── Missing file is not corruption ───────────────────────────────────────────

def test_missing_file_returns_default_and_quarantines_nothing(tmp_path):
    """A first run has no state file. That must stay silent under both modes —
    quarantining a file that was never written would be pure noise."""
    p = str(tmp_path / "absent.json")
    assert state_io.read_json(p, default={"d": 1}) == {"d": 1}
    assert state_io.read_json(p, default={"d": 1}, strict=True) == {"d": 1}
    assert _corrupt_files(tmp_path) == []


# ── Corrupt file: strict raises, both modes quarantine ───────────────────────

@pytest.mark.parametrize("bad", ['{"halted": tr', "", "not json at all", '{"a": 1}{"b": 2}'])
def test_strict_raises_and_quarantines_exactly_one_file(tmp_path, bad):
    p = tmp_path / "risk_state.json"
    p.write_text(bad)
    with pytest.raises(StateCorruption):
        state_io.read_json(str(p), default={}, strict=True)
    assert len(_corrupt_files(tmp_path)) == 1


def test_lenient_returns_default_but_still_quarantines(tmp_path):
    """strict=False keeps the old contract for callers that genuinely don't
    care, but the evidence is preserved either way."""
    p = tmp_path / "cache.json"
    p.write_text("{ truncated")
    assert state_io.read_json(str(p), default=[]) == []
    assert len(_corrupt_files(tmp_path)) == 1


def test_quarantine_preserves_the_bytes(tmp_path):
    p = tmp_path / "evidence.json"
    p.write_text("{ half a write")
    state_io.read_json(str(p), default={})
    (quarantined,) = _corrupt_files(tmp_path)
    assert open(quarantined).read() == "{ half a write"


def test_original_path_is_gone_so_next_write_starts_clean(tmp_path):
    """Leaving the bad file in place would make every subsequent read fail
    identically — the bot would never recover on its own."""
    p = tmp_path / "state.json"
    p.write_text("garbage")
    state_io.read_json(str(p), default={})
    assert not p.exists()

    state_io.atomic_write_json(str(p), {"clean": True})
    assert state_io.read_json(str(p), strict=True) == {"clean": True}


def test_error_message_names_the_quarantine_path(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("garbage")
    with pytest.raises(StateCorruption) as exc:
        state_io.read_json(str(p), strict=True)
    assert ".corrupt." in str(exc.value)


# ── update_json forwards strict ──────────────────────────────────────────────

def test_update_json_strict_raises_before_the_mutator_runs(tmp_path):
    """Without the passthrough, the mutator would receive `default` and write a
    freshly-rebuilt file over the corruption — erasing exactly what the read was
    supposed to protect."""
    p = tmp_path / "counter.json"
    p.write_text("{ nope")
    ran = []
    with pytest.raises(StateCorruption):
        state_io.update_json(str(p), lambda s: ran.append(s) or {}, default={}, strict=True)
    assert ran == []


def test_update_json_lenient_still_rebuilds(tmp_path):
    p = tmp_path / "counter.json"
    p.write_text("{ nope")
    assert state_io.update_json(str(p), lambda s: {"n": s.get("n", 0) + 1},
                                default={}) == {"n": 1}


def test_update_json_releases_the_lock_on_corruption(tmp_path):
    """The raise happens inside file_lock — if it leaked the lock file, every
    later writer would block until the stale timeout."""
    p = tmp_path / "state.json"
    p.write_text("garbage")
    with pytest.raises(StateCorruption):
        state_io.update_json(str(p), lambda s: s, default={}, strict=True)
    assert not os.path.exists(str(p) + ".lock")


# ── Regression: the halt flag cannot silently disappear ──────────────────────

def test_corrupt_risk_state_never_reads_as_not_halted(tmp_path, monkeypatch):
    """The dangerous one, stated as the invariant: a corrupt risk_state.json
    must NEVER produce a dict whose halt flag is simply absent, because every
    caller reads that as `not halted` and resumes trading."""
    monkeypatch.chdir(tmp_path)
    import risk_manager as rm

    (tmp_path / "risk_state.json").write_text('{"date": "2026-07-29", "halted": tr')

    with pytest.raises(StateCorruption):
        rm.load_state()


def test_risk_state_wrong_json_type_is_corruption(tmp_path, monkeypatch):
    """Valid JSON that isn't an object still can't be used as state — .get()
    would explode later, or worse, silently succeed on a dict-like."""
    monkeypatch.chdir(tmp_path)
    import risk_manager as rm

    (tmp_path / "risk_state.json").write_text('["not", "a", "state"]')
    with pytest.raises(StateCorruption):
        rm.load_state()


def test_healthy_risk_state_still_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import risk_manager as rm
    from utils import today_est

    state = {"date": today_est().isoformat(), "realized_pnl": -50.0,
             "trades_today": 3, "halted": True}
    (tmp_path / "risk_state.json").write_text(json.dumps(state))
    assert rm.load_state() == state


def test_missing_risk_state_still_returns_fresh_default(tmp_path, monkeypatch):
    """First run of the day must not be mistaken for corruption."""
    monkeypatch.chdir(tmp_path)
    import risk_manager as rm

    s = rm.load_state()
    assert s["halted"] is False and s["trades_today"] == 0


# ── Regression: the pending-order ledger ─────────────────────────────────────

def test_corrupt_pending_ledger_raises(tmp_path, monkeypatch):
    """Reading a corrupt ledger as {} tells recovery there is nothing in
    flight, orphaning a real broker fill with no local position."""
    import order_manager as om

    p = tmp_path / "pending.json"
    p.write_text("{ mangled")
    monkeypatch.setattr(om, "PENDING_FILE", str(p))
    with pytest.raises(StateCorruption):
        om._load_pending()


def test_missing_pending_ledger_is_empty(tmp_path, monkeypatch):
    import order_manager as om
    monkeypatch.setattr(om, "PENDING_FILE", str(tmp_path / "pending.json"))
    assert om._load_pending() == {}
    assert om.recover_pending_orders() == []


# ── The startup abort ────────────────────────────────────────────────────────

def test_abort_on_corruption_exits_1_and_alerts(monkeypatch, capsys):
    """A headless strategy under start_all.py must not die quietly — a log line
    alone is read tomorrow, by which point the operator has spent a session
    believing a stopped bot was trading."""
    sent = []
    import event_notifier
    monkeypatch.setattr(event_notifier, "_dispatch",
                        lambda **kw: sent.append(kw))

    with pytest.raises(SystemExit) as exc:
        state_io.abort_on_corruption(StateCorruption("boom"), "hft_executor")
    assert exc.value.code == 1
    assert len(sent) == 1
    assert sent[0]["severity"] == "danger"
    assert "hft_executor" in sent[0]["subject"]
    assert "boom" in capsys.readouterr().err


def test_abort_on_corruption_exits_even_if_the_notifier_is_down(monkeypatch):
    """A broken alert channel must not swallow the exit."""
    import event_notifier
    def _explode(**kw):
        raise RuntimeError("telegram unreachable")
    monkeypatch.setattr(event_notifier, "_dispatch", _explode)

    with pytest.raises(SystemExit) as exc:
        state_io.abort_on_corruption(StateCorruption("boom"), "executor")
    assert exc.value.code == 1
