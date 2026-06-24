"""
decision_log.py

Append-only log of every meaningful decision the bot makes.

The bot already computes a reason for every action (blocked by risk
manager: …, no qualifying contract, considered but score too low, …)
and prints it to the console. We were throwing all of that away.

This module persists those decisions to a JSONL file so the dashboard's
Decision Log tab can answer the single most important question:
"Why did the bot do — or NOT do — what it did?"

Format: one JSON object per line (JSONL is append-friendly and resilient
to crashes mid-write, unlike a single-file JSON array).
"""

import json
import os
import datetime
from typing import Any

DECISION_LOG_FILE = "decision_log.jsonl"

# Standard action vocabulary — keeps the dashboard's filter dropdown clean.
ACTION_TRADED       = "traded"
ACTION_REJECTED     = "rejected"
ACTION_CONSIDERED   = "considered"   # scanned but didn't meet score threshold
ACTION_SKIPPED      = "skipped"      # didn't even get scanned (e.g. market closed)
ACTION_EXITED       = "exited"
ACTION_HALT         = "halt"


# In-memory dedup: the scanner re-evaluates the same handful of tickers every
# cycle and would otherwise write the identical "considered: score 45 < 50"
# line dozens of times, drowning the log. We remember the last decision per
# (ticker, strategy) and skip writing an identical repeat. The cache is
# per-process and resets on restart (so you still get one fresh entry per
# ticker after a restart), which is the desired behaviour.
_last_decision: dict[tuple[str, str], tuple[str, str]] = {}


def log_decision(
    ticker: str,
    action: str,
    reason: str,
    score: float | None = None,
    strategy: str = "catalyst_long_call",
    extras: dict[str, Any] | None = None,
    force: bool = False,
) -> None:
    """
    Append a decision record. Never raises — logging must never crash the bot.

    Consecutive identical decisions for the same ticker are skipped (only a
    CHANGE in action/reason is logged) so the log stays readable instead of
    filling with one ticker repeated every scan cycle. Pass force=True to
    always write (e.g. for traded/exited events you never want collapsed).
    """
    key = (ticker, strategy)
    sig = (action, reason)
    if not force and _last_decision.get(key) == sig:
        return  # identical to this ticker's last decision — don't spam the log
    _last_decision[key] = sig

    record = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "ticker":    ticker,
        "strategy":  strategy,
        "action":    action,
        "reason":    reason,
        "score":     score,
    }
    if extras:
        record.update(extras)

    try:
        with open(DECISION_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        # Logging failures must never break trading.
        print(f"[DecisionLog] Could not write entry: {e}")


def compact_log() -> int:
    """
    One-time cleanup: collapse runs of identical consecutive decisions per
    ticker in the EXISTING log file (the in-memory dedup only affects new
    writes). Keeps the first entry of each new (ticker, action, reason) run.

    Returns the number of entries removed.
    """
    if not os.path.exists(DECISION_LOG_FILE):
        return 0
    try:
        with open(DECISION_LOG_FILE, "r") as f:
            records = [json.loads(l) for l in f if l.strip()]
    except Exception as e:
        print(f"[DecisionLog] compact: could not read log: {e}")
        return 0

    kept: list[dict] = []
    last: dict[tuple, tuple] = {}
    for r in records:
        key = (r.get("ticker"), r.get("strategy"))
        sig = (r.get("action"), r.get("reason"))
        if last.get(key) == sig:
            continue   # identical to this ticker's previous kept decision
        last[key] = sig
        kept.append(r)

    removed = len(records) - len(kept)
    if removed:
        try:
            tmp = DECISION_LOG_FILE + ".tmp"
            with open(tmp, "w") as f:
                for r in kept:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, DECISION_LOG_FILE)
        except Exception as e:
            print(f"[DecisionLog] compact: could not rewrite log: {e}")
            return 0
    return removed


def load_recent_decisions(limit: int = 200) -> list[dict]:
    """Return the last `limit` decisions for dashboard rendering."""
    if not os.path.exists(DECISION_LOG_FILE):
        return []
    try:
        with open(DECISION_LOG_FILE, "r") as f:
            lines = f.readlines()
        records = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception as e:
        print(f"[DecisionLog] Could not read log: {e}")
        return []
