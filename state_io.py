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

FAIL-CLOSED READS
-----------------
Atomic writes close the path that CREATES corruption. The read path used to
reopen the same failure: read_json swallowed every exception and returned
`default`, so a corrupt risk_state.json silently became {} — halt flag gone,
trades_today reset, daily P&L reset — and the bot happily kept trading.

`strict=True` makes an unparseable file raise StateCorruption instead. Callers
that own a safety-critical file (risk_state.json, pending_orders.json) pass it;
a blank risk state is far more dangerous than a stopped bot. Either way the bad
file is renamed to `<path>.corrupt.<unix_ts>` so the evidence survives and the
next write starts from clean ground.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import contextlib
from typing import Any, Callable

_LOG = None


def _log():
    """
    Lazy, memoised logger.

    state_io is the leaf module everything else imports, and get_logger() opens
    a rotating file handler on first call — merely importing this module should
    not create a log file.
    """
    global _LOG
    if _LOG is None:
        from logger import get_logger
        _LOG = get_logger("state_io")
    return _LOG


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


class StateCorruption(RuntimeError):
    """A state file exists but could not be read or parsed.

    Distinct from "file missing", which is a legitimate first run and always
    yields `default`. This means bytes are on disk that we cannot trust.
    """


def _quarantine(path: str) -> str | None:
    """
    Rename an unreadable state file aside so it can be inspected later.

    Deleting it would destroy the only evidence of what went wrong, and leaving
    it in place would make every subsequent read fail identically. Moving it
    does both jobs: the next write starts clean, and a post-mortem still has
    the bytes. Returns the new path, or None if the rename didn't happen.
    """
    dest = f"{path}.corrupt.{int(time.time())}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        # Another process may have already quarantined or replaced it, or the
        # file is locked. Not worth failing over — the caller is already on an
        # error path and `strict` decides what happens next.
        return None


def read_json(path: str, default: Any = None, *, strict: bool = False) -> Any:
    """
    Read JSON from `path`.

    Missing file           → `default` (a legitimate first run).
    Present but unreadable → quarantined to `<path>.corrupt.<unix_ts>`, then
                             StateCorruption if `strict`, else `default`.

    `strict` defaults to False so existing callers are unaffected. Pass it for
    any file whose absence changes a safety decision — silently substituting {}
    for a corrupt risk_state.json is how a daily-loss halt disappears.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        # Raced with a rename or delete between the exists() check and the
        # open(). Nothing is corrupt — treat it as the missing-file case.
        return default
    except OSError as e:
        # Locked, permission-denied, bad sector. The content may be perfectly
        # fine, so don't quarantine — but don't pretend it's empty either.
        if strict:
            raise StateCorruption(f"could not read {path}: {e}") from e
        return default

    try:
        return json.loads(raw)
    except ValueError as e:
        dest = _quarantine(path)
        detail = f"quarantined to {dest}" if dest else "could not be quarantined"
        if strict:
            raise StateCorruption(f"{path} is not valid JSON ({e}); {detail}") from e
        return default


# ─── Cross-process file lock ──────────────────────────────────────────────────

class LockTimeout(RuntimeError):
    pass


def _read_lock_token(lock_path: str) -> str | None:
    """Current contents of a lock file, or None if it's gone/unreadable."""
    try:
        with open(lock_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0, stale_after: float = 60.0):
    """
    Acquire an advisory lock for `path` by creating `<path>.lock` exclusively.

    - Blocks (spin-waits) up to `timeout` seconds for the lock.
    - If an existing lock file is older than `stale_after` seconds it's assumed
      to be from a crashed process and is broken, so a dead process can't
      deadlock the whole bot.

    OWNERSHIP TOKENS
    ----------------
    The lock file carries `<pid>:<uuid4>`, unique per acquisition. Without proof
    of ownership the lock had two races, both of which let two processes believe
    they held it exclusively:

      * Stale-breaking was TOCTOU. A sees the lock is stale, removes it,
        recreates it, and proceeds. B — which read getmtime moments earlier —
        then removes A's *fresh* lock and acquires. So a stale lock is only
        removed if its contents still match what the staleness check observed;
        a change means someone else took over, and we go back to waiting.

      * Release was unconditional. Any mutator running longer than
        `stale_after` gets its own lock broken, and its `finally` then deleted
        the NEW holder's lock. So release only removes the file if it still
        contains this acquisition's token.

    RULE: never do network or broker I/O inside an `update_json` mutator (or
    any file_lock body). A wedged HTTP call blows through `stale_after` and gets
    the lock broken underneath the holder, which is the exact scenario the
    conditional release above turns from silent corruption into a warning.

    FUTURE WORK: OS-level locking (`fcntl.flock` / `msvcrt.locking`) is strictly
    more correct — the kernel releases the lock when the process dies, which
    removes the stale-lock concept and both of the races above along with it.
    That is a larger change than this file should absorb in one go.

    Usage:
        with file_lock("risk_state.json"):
            ... read-modify-write ...
    """
    lock_path = f"{path}.lock"
    # Unique per ACQUISITION, not per process: one process may lock the same
    # file repeatedly, and a recycled pid must not look like a live holder.
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.time() + timeout
    acquired = False

    while True:
        try:
            # O_CREAT | O_EXCL fails if the lock file already exists → that's
            # how we detect contention without a race.
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, token.encode())
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
                    # Read the contents ONLY on the stale path. On Windows an
                    # open handle blocks the holder's own os.remove, so opening
                    # this file on every 50ms retry would turn a healthy release
                    # into a spin that lasts until someone's timeout.
                    observed = _read_lock_token(lock_path)
                    # Re-read immediately before removing. If the holder changed
                    # in the meantime, someone else already broke and re-took
                    # this lock — removing now would delete THEIR live lock.
                    if observed is not None and _read_lock_token(lock_path) == observed:
                        with contextlib.suppress(OSError):
                            os.remove(lock_path)
                        continue  # retry immediately
                    # Ownership moved on. Fall through and keep waiting.
            except FileNotFoundError:
                continue  # it vanished — retry immediately
            except PermissionError:
                pass       # can't stat it right now — just wait and retry
            if time.time() >= deadline:
                raise LockTimeout(f"Could not acquire lock for {path} within {timeout}s")
            time.sleep(0.05)

    held_since = time.time()
    try:
        yield
    finally:
        if acquired:
            held = time.time() - held_since
            if held > stale_after / 2:
                # Surface long holds while they are still merely slow. Past
                # stale_after another process breaks this lock, and the first
                # visible symptom would otherwise be mystery corruption.
                _log().warning(
                    "Held lock on %s for %.1fs (stale_after=%.0fs) — another process "
                    "will break it past that. Is there network I/O inside the mutator?",
                    path, held, stale_after,
                )
            # Only remove OUR lock. If this acquisition was broken as stale, the
            # file now belongs to a different holder and deleting it would hand
            # the same lock to a third process.
            if _read_lock_token(lock_path) == token:
                # OSError covers both the already-gone case and Windows'
                # sharing violation when a waiter has the file open; a lock left
                # behind here is recovered by the next holder's stale-break.
                with contextlib.suppress(OSError):
                    os.remove(lock_path)
            else:
                _log().warning(
                    "Lock on %s was taken over by another process while held — "
                    "not removing it. This run's write may have raced.", path,
                )


def update_json(
    path: str,
    mutator: Callable[[Any], Any],
    default: Any = None,
    timeout: float = 10.0,
    *,
    strict: bool = False,
) -> Any:
    """
    Safely read-modify-write a JSON file across processes.

    Acquires the lock, reads the current value (or `default`), passes it to
    `mutator`, atomically writes whatever the mutator returns, and returns it.

    `strict` is forwarded to read_json: with it set, a corrupt file raises
    StateCorruption instead of feeding `default` to the mutator — which would
    otherwise quietly rebuild the file from nothing, erasing whatever the
    corrupt copy was supposed to be protecting.

    Example — increment a counter without losing updates:
        update_json("risk_state.json",
                    lambda s: {**s, "trades_today": s.get("trades_today", 0) + 1},
                    default={}, strict=True)
    """
    with file_lock(path, timeout=timeout):
        current = read_json(path, default, strict=strict)
        new_value = mutator(current)
        atomic_write_json(path, new_value)
        return new_value


def abort_on_corruption(exc: StateCorruption, component: str) -> None:
    """
    Report a fatal state corruption on every channel available, then exit(1).

    Called from a strategy's startup path. The alert matters more than the log
    here: the strategies run headless under start_all.py, so a message that only
    reaches a log file is a message the operator reads tomorrow — by which point
    they have spent a session believing a halted bot was trading, or the reverse.

    event_notifier is imported lazily and best-effort. state_io is a leaf module
    that everything else depends on, so a module-level import would invert the
    dependency; and a notifier that is down must not swallow the exit.
    """
    _log().error("FATAL — corrupt state file in %s: %s", component, exc)
    print(f"\n[{component}] FATAL: {exc}", file=sys.stderr)
    print(f"[{component}] Refusing to start on unreadable state. Inspect the "
          f".corrupt.* file, fix or remove it, then restart.\n", file=sys.stderr)
    try:
        from event_notifier import _dispatch
        _dispatch(
            subject=f"State corruption — {component} will not start",
            lines=[f"{component} found an unreadable state file and exited.",
                   f"Detail: {exc}",
                   "No positions are being managed by this strategy until it restarts."],
            severity="danger",
        )
    except Exception as e:  # noqa: BLE001 — never let the alert mask the exit
        print(f"[{component}] could not send corruption alert: {e}", file=sys.stderr)
    sys.exit(1)
