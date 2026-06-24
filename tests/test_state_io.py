"""Tests for state_io: atomic writes, safe reads, locking, update_json."""

import os
import json
import multiprocessing

import state_io


def test_atomic_write_and_read(tmp_path):
    p = str(tmp_path / "x.json")
    state_io.atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert state_io.read_json(p) == {"a": 1, "b": [1, 2, 3]}


def test_read_missing_returns_default(tmp_path):
    p = str(tmp_path / "missing.json")
    assert state_io.read_json(p, default={"d": True}) == {"d": True}


def test_atomic_write_rejects_infinity(tmp_path):
    """inf/nan are invalid JSON and break the browser parser — must fail loud,
    and must NOT leave a corrupt file behind."""
    import pytest
    p = str(tmp_path / "inf.json")
    with pytest.raises(ValueError):
        state_io.atomic_write_json(p, {"x": float("inf")})
    assert not __import__("os").path.exists(p)


def test_atomic_write_rejects_nan(tmp_path):
    import pytest
    p = str(tmp_path / "nan.json")
    with pytest.raises(ValueError):
        state_io.atomic_write_json(p, {"x": float("nan")})


def test_read_corrupt_returns_default(tmp_path):
    p = str(tmp_path / "corrupt.json")
    with open(p, "w") as f:
        f.write("{ not valid json ")
    assert state_io.read_json(p, default=[]) == []


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    p = str(tmp_path / "y.json")
    state_io.atomic_write_json(p, {"ok": 1})
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftover == []


def test_update_json_mutator(tmp_path):
    p = str(tmp_path / "counter.json")
    state_io.atomic_write_json(p, {"n": 0})
    result = state_io.update_json(p, lambda s: {"n": s["n"] + 5}, default={"n": 0})
    assert result == {"n": 5}
    assert state_io.read_json(p) == {"n": 5}


def test_file_lock_is_exclusive(tmp_path):
    """A second acquire while the first is held must time out."""
    p = str(tmp_path / "locked.json")
    with state_io.file_lock(p, timeout=2.0):
        # Lock file should exist while held
        assert os.path.exists(p + ".lock")
        try:
            with state_io.file_lock(p, timeout=0.3):
                assert False, "second lock should not have been acquired"
        except state_io.LockTimeout:
            pass
    # Released → lock file gone
    assert not os.path.exists(p + ".lock")


def test_file_lock_breaks_stale(tmp_path):
    """A lock file older than stale_after is broken and re-acquired."""
    p = str(tmp_path / "stale.json")
    lock = p + ".lock"
    with open(lock, "w") as f:
        f.write("99999 0")  # ancient timestamp content; mtime is now though
    # Force mtime into the past
    old = os.path.getmtime(lock) - 120
    os.utime(lock, (old, old))
    with state_io.file_lock(p, timeout=2.0, stale_after=60):
        assert os.path.exists(lock)  # we now hold it
    assert not os.path.exists(lock)


def _incr_worker(path, iterations):
    import state_io as sio
    for _ in range(iterations):
        sio.update_json(path, lambda s: {"n": s.get("n", 0) + 1}, default={"n": 0})


def test_concurrent_updates_no_lost_writes(tmp_path):
    """4 processes × 25 increments must total exactly 100 (lock prevents loss)."""
    p = str(tmp_path / "shared.json")
    state_io.atomic_write_json(p, {"n": 0})
    procs = [multiprocessing.Process(target=_incr_worker, args=(p, 25)) for _ in range(4)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    assert state_io.read_json(p)["n"] == 100
