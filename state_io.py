"""
state_io.py — safe JSON state I/O for a multi-process bot.

start_all.py runs the three strategy executors as SEPARATE processes. They
all read and write the same JSON state files:

    risk_state.json        record_trade()/halt — read-modify-write (dangerous)
    closed_trades.json     every strategy appends closed trades
    equity_curve.json      equity snapshots
    dashboard_state.json   dashboard snapshot
    pending_orders.json    order ledger

Two failure modes without coordination:

  1. CORRUPTION / PARTIAL READS — process A is halfway through writing a file
     when process B reads it, or two writes interleave. A corrupt
     risk_state.json could silently disable the daily-loss halt. This is the
     dangerous one.

  2. LOST UPDATES — A and B both read trades_today=5, both write 6; one
     increment is lost. Annoying but not dangerous.

This module fixes both:

  * atomic_write_json()  — write to a temp file then os.replace() (atomic on
    Windows and POSIX), so a reader never sees a half-written file and
    concurrent writers degrade to last-writer-wins instead of corruption.

  * file_lock()          — cross-platform advisory lock via an exclusive lock
    file with a stale-lock timeout. Wrap a read-modify-write in it to make the
    whole sequence atomic across processes.

  * update_json()        — lock + read + mutate + atomic-write + unlock, the
    safe primitive for counters and append-only lists.
"""

from __future__ import annotations

import json
import os
import time
import contextlib
from typing import Any, Callable


# ─── Atomic write / safe read ─────────────────────────────────────────────────

def atomic_write_json(path: str, data: Any) -> None:
    """
    Write `data` as JSON to `path` atomically.

    Writes to a uniquely-named temp file in the same directory, flushes+fsyncs,
    then os.replace()s it over the target. os.replace is atomic on the same
    filesystem on both Windows and POSIX, so readers always see either the old
    or the new file — never a partial write.
    """
    directory = os.path.dirname(os.path.abspath(path))
    tmp = f"{path}.{os.getpid()}.{int(time.time()*1000)}.tmp"
    tmp = os.path.join(directory, os.path.basename(tmp))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # allow_nan=False: refuse to emit Infinity/NaN, which are invalid
            # JSON and break strict parsers (e.g. the browser's JSON.parse on
            # dashboard_state.json). Fail loud here rather than corrupt the file.
            json.dump(data, f, indent=2, default=str, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(Exception):
            if os.path.exists(tmp):
                os.remove(tmp)
        raise


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from `path`, returning `default` on any error/missing file."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ─── Cross-process file lock ──────────────────────────────────────────────────

class LockTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0, stale_after: float = 60.0):
    """
    Acquire an advisory lock for `path` by creating `<path>.lock` exclusively.

    - Blocks (spin-waits) up to `timeout` seconds for the lock.
    - If an existing lock file is older than `stale_after` seconds it's assumed
      to be from a crashed process and is broken, so a dead process can't
      deadlock the whole bot.

    Usage:
        with file_lock("risk_state.json"):
            ... read-modify-write ...
    """
    lock_path = f"{path}.lock"
    deadline = time.time() + timeout
    acquired = False

    while True:
        try:
            # O_CREAT | O_EXCL fails if the lock file already exists → that's
            # how we detect contention without a race.
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            acquired = True
            break
        except (FileExistsError, PermissionError):
            # FileExistsError → another holder. PermissionError → Windows
            # reports this when the lock file is mid-deletion ("delete pending")
            # as another process releases it; treat it as transient contention.
            # Break the lock only if it's genuinely stale.
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > stale_after:
                    with contextlib.suppress(Exception):
                        os.remove(lock_path)
                    continue  # retry immediately
            except FileNotFoundError:
                continue  # it vanished — retry immediately
            except PermissionError:
                pass       # can't stat it right now — just wait and retry
            if time.time() >= deadline:
                raise LockTimeout(f"Could not acquire lock for {path} within {timeout}s")
            time.sleep(0.05)

    try:
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                os.remove(lock_path)


def update_json(
    path: str,
    mutator: Callable[[Any], Any],
    default: Any = None,
    timeout: float = 10.0,
) -> Any:
    """
    Safely read-modify-write a JSON file across processes.

    Acquires the lock, reads the current value (or `default`), passes it to
    `mutator`, atomically writes whatever the mutator returns, and returns it.

    Example — increment a counter without losing updates:
        update_json("risk_state.json",
                    lambda s: {**s, "trades_today": s.get("trades_today", 0) + 1},
                    default={})
    """
    with file_lock(path, timeout=timeout):
        current = read_json(path, default)
        new_value = mutator(current)
        atomic_write_json(path, new_value)
        return new_value
