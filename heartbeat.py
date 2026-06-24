"""
heartbeat.py — liveness signals for each strategy process.

start_all.py already notices when a strategy process *exits*. It cannot tell
when one is still alive but *stuck* — wedged on a hung network call, an
infinite retry, or a deadlock. A hung executor is dangerous: it stops managing
open positions (no stop-losses fire) while looking healthy.

The fix is a heartbeat. Each strategy calls beat() once per loop iteration,
writing a tiny timestamped file. A monitor (the watchdog, or start_all.py)
reads those files and flags any strategy whose heartbeat has gone stale.

API
---
    beat(strategy, status="running", **extra)   call once per loop
    read_heartbeats() -> {strategy: record}
    stale_heartbeats(max_age_seconds) -> [(strategy, age, record), ...]

Files live in heartbeats/<strategy>.json and are written atomically.
"""

from __future__ import annotations

import os
import time
import datetime

from state_io import atomic_write_json, read_json

HEARTBEAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeats")


def _path(strategy: str) -> str:
    safe = "".join(c for c in strategy if c.isalnum() or c in "-_") or "unknown"
    return os.path.join(HEARTBEAT_DIR, f"{safe}.json")


def beat(strategy: str, status: str = "running", **extra) -> None:
    """
    Record a heartbeat for `strategy`. Call once per main-loop iteration.

    `status` is a free-form string (e.g. "scanning", "idle", "managing").
    `extra` is merged in for context (open positions, cycle number, etc.).
    Never raises — a heartbeat failure must not crash the strategy.
    """
    try:
        os.makedirs(HEARTBEAT_DIR, exist_ok=True)
        record = {
            "strategy":  strategy,
            "status":    status,
            "ts":        time.time(),
            "iso":       datetime.datetime.now().isoformat(timespec="seconds"),
            "pid":       os.getpid(),
            **extra,
        }
        atomic_write_json(_path(strategy), record)
    except Exception:
        pass  # liveness signalling is best-effort


def read_heartbeats() -> dict[str, dict]:
    """Return {strategy: latest_record} for every heartbeat file present."""
    out: dict[str, dict] = {}
    if not os.path.isdir(HEARTBEAT_DIR):
        return out
    for fn in os.listdir(HEARTBEAT_DIR):
        if not fn.endswith(".json"):
            continue
        rec = read_json(os.path.join(HEARTBEAT_DIR, fn), None)
        if isinstance(rec, dict) and rec.get("strategy"):
            out[rec["strategy"]] = rec
    return out


def stale_heartbeats(max_age_seconds: float) -> list[tuple[str, float, dict]]:
    """
    Return [(strategy, age_seconds, record), ...] for every heartbeat older
    than `max_age_seconds`. A strategy that's deliberately idle (status="idle")
    still beats every cycle, so staleness genuinely means "stopped beating".
    """
    now = time.time()
    stale = []
    for strategy, rec in read_heartbeats().items():
        ts = float(rec.get("ts", 0) or 0)
        age = now - ts
        if age > max_age_seconds:
            stale.append((strategy, age, rec))
    return stale


def clear(strategy: str) -> None:
    """Remove a strategy's heartbeat file (e.g. on clean shutdown)."""
    try:
        p = _path(strategy)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


if __name__ == "__main__":
    # Quick status view.
    hbs = read_heartbeats()
    if not hbs:
        print("No heartbeats found.")
    else:
        now = time.time()
        print(f"{'STRATEGY':<16}{'STATUS':<12}{'AGE':<10}{'PID':<8}")
        for s, rec in sorted(hbs.items()):
            age = now - float(rec.get("ts", 0) or 0)
            print(f"{s:<16}{rec.get('status', '?'):<12}{age:>6.0f}s   {rec.get('pid', '?')}")
