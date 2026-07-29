"""
The file lock needs proof of ownership, not just presence.

Without a token the lock file says only "someone holds this", which allowed two
processes to hold the same lock simultaneously by two different routes:

  * Stale-breaking was TOCTOU. A reads getmtime, sees stale, removes, recreates,
    proceeds. B — holding a getmtime read from moments earlier — then removes
    A's FRESH lock and acquires it too.

  * Release was unconditional. Any mutator that runs longer than stale_after
    gets its lock broken by a waiter; its own `finally` then deletes the new
    holder's lock, handing the same lock to a third process.

Both are silent. The symptom is a lost update or a corrupt file much later, with
nothing linking it back here.
"""

import os
import threading
import time
import multiprocessing

import pytest

import state_io


class _Recorder:
    """Stands in for state_io's logger so warnings are assertable."""

    def __init__(self):
        self.warnings = []

    def warning(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)

    def error(self, fmt, *args):
        pass


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(state_io, "_log", lambda: rec)
    return rec


# ── The token itself ─────────────────────────────────────────────────────────

def test_lock_file_carries_pid_and_a_unique_token(tmp_path):
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p):
        first = open(p + ".lock").read()
    with state_io.file_lock(p):
        second = open(p + ".lock").read()

    pid = str(os.getpid())
    assert first.startswith(pid + ":") and second.startswith(pid + ":")
    # Unique per ACQUISITION, not per process — one process locks the same file
    # many times, and a recycled pid must not read as a live holder.
    assert first != second


def test_normal_release_removes_our_own_lock(tmp_path):
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p):
        assert os.path.exists(p + ".lock")
    assert not os.path.exists(p + ".lock")


# ── Race 1: release must not delete someone else's lock ──────────────────────

def test_release_does_not_delete_a_lock_taken_over_by_another_process(tmp_path, recorder):
    """This holder ran past stale_after, a waiter broke its lock and took over.
    The holder's finally must NOT delete the new owner's lock."""
    p = str(tmp_path / "s.json")
    lock = p + ".lock"

    with state_io.file_lock(p):
        # Another process breaks our stale lock and writes its own token.
        with open(lock, "w") as f:
            f.write("99999:other-process-token")

    assert os.path.exists(lock), "deleted a lock this process no longer owned"
    assert open(lock).read() == "99999:other-process-token"
    assert any("taken over" in w for w in recorder.warnings)


def test_release_tolerates_an_already_deleted_lock(tmp_path):
    """FileNotFoundError on release must be suppressed, not propagate out of
    the context manager and mask the body's own result."""
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p):
        os.remove(p + ".lock")
    assert not os.path.exists(p + ".lock")


# ── Race 2: stale-break must re-verify before removing ───────────────────────

def _make_stale(lock_path, contents, stale_after):
    with open(lock_path, "w") as f:
        f.write(contents)
    old = os.path.getmtime(lock_path) - (stale_after * 2)
    os.utime(lock_path, (old, old))


def test_stale_lock_with_stable_contents_is_broken(tmp_path):
    """The baseline the re-verify must not regress: a genuinely abandoned lock
    still gets broken, or a crashed process deadlocks the whole bot."""
    p = str(tmp_path / "s.json")
    lock = p + ".lock"
    _make_stale(lock, "1234:dead-process", stale_after=60)

    with state_io.file_lock(p, timeout=2.0, stale_after=60):
        assert open(lock).read().startswith(str(os.getpid()) + ":")
    assert not os.path.exists(lock)


def test_stale_lock_is_not_removed_if_its_contents_changed(tmp_path, monkeypatch):
    """Between the staleness check and the removal, another waiter broke and
    re-took the lock. Removing now would delete that live lock and let two
    processes into the critical section."""
    p = str(tmp_path / "s.json")
    lock = p + ".lock"
    _make_stale(lock, "1234:dead-process", stale_after=60)

    reads = []
    real = state_io._read_lock_token

    def _shifting(lock_path):
        reads.append(lock_path)
        if len(reads) == 1:
            return "1234:dead-process"      # the staleness observation
        # The re-verify. Simulate a competitor that broke and re-took the lock
        # in between: new contents AND a new mtime, exactly as a real
        # acquisition leaves it.
        with open(lock_path, "w") as f:
            f.write("5678:new-holder")
        return "5678:new-holder"

    monkeypatch.setattr(state_io, "_read_lock_token", _shifting)

    with pytest.raises(state_io.LockTimeout):
        with state_io.file_lock(p, timeout=0.4, stale_after=60):
            pass

    monkeypatch.setattr(state_io, "_read_lock_token", real)
    assert os.path.exists(lock), "removed a lock another process had taken over"


# ── Timeout behaviour is unchanged ───────────────────────────────────────────

def test_fresh_lock_held_past_timeout_still_raises(tmp_path):
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p, timeout=2.0):
        with pytest.raises(state_io.LockTimeout):
            with state_io.file_lock(p, timeout=0.3):
                pass


# ── Long-hold warning ────────────────────────────────────────────────────────

def test_warns_when_held_past_half_of_stale_after(tmp_path, recorder):
    """Past stale_after another process breaks this lock. Surfacing the long
    hold while it is merely slow beats debugging the corruption it causes."""
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p, stale_after=0.10):
        time.sleep(0.12)
    assert any("Held lock on" in w for w in recorder.warnings)


def test_no_warning_for_a_normal_short_hold(tmp_path, recorder):
    p = str(tmp_path / "s.json")
    with state_io.file_lock(p, stale_after=60):
        pass
    assert recorder.warnings == []


# ── No lost updates ──────────────────────────────────────────────────────────

def _incr(path, iterations):
    import state_io as sio
    for _ in range(iterations):
        # Generous timeout: on Windows, spawn start-up plus four-way contention
        # can outlast the 10s default, and a LockTimeout here would be a test
        # harness artefact rather than a lost update.
        sio.update_json(path, lambda s: {"n": s.get("n", 0) + 1},
                        default={"n": 0}, timeout=60.0)


def test_concurrent_thread_updates_lose_nothing(tmp_path):
    """Threads share a pid, so a pid-only token would make every thread look
    like the same holder. The uuid half is what keeps them distinct."""
    p = str(tmp_path / "shared.json")
    state_io.atomic_write_json(p, {"n": 0})
    threads = [threading.Thread(target=_incr, args=(p, 20)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state_io.read_json(p)["n"] == 80


def test_concurrent_process_updates_lose_nothing(tmp_path):
    p = str(tmp_path / "shared.json")
    state_io.atomic_write_json(p, {"n": 0})
    procs = [multiprocessing.Process(target=_incr, args=(p, 15)) for _ in range(4)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    assert state_io.read_json(p)["n"] == 60
