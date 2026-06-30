"""
position_book.py — shared single-leg position-book helpers.

Three strategy executors keep their own JSON book of open single-leg option
positions, each in its own file (hft_positions.json, pead_positions.json,
bounce_positions.json):

    Strategy 3  hft_executor.py     — intraday 0-DTE scalps
    Strategy 4  pead_executor.py    — post-earnings / news-drift swings
    Strategy 5  bounce_executor.py  — bear-regime capitulation bounces

Their load/save/add/remove/update logic was byte-identical except for the
filename, so it lives here once and each executor binds it to its own file with
thin module-level wrappers (which keeps every existing call site unchanged).

Concurrency: each executor is the ONLY writer of its own book (a separate
process), and the dashboard only READS it, so a plain read-modify-write is
race-free. The write itself still goes through state_io.atomic_write_json so a
reader never catches a half-written file.

Multi-leg books (iv_rank's spreads/condors) are NOT handled here — they need
per-leg structure and live in iv_rank_bot.py with their own locked helpers.
"""

from __future__ import annotations

from mawitek.infra.state_io import atomic_write_json, read_json


def load(state_file: str) -> list[dict]:
    """All open positions in `state_file`. [] if missing or unreadable."""
    data = read_json(state_file, [])
    return data if isinstance(data, list) else []


def save(state_file: str, positions: list[dict]) -> None:
    """Atomically overwrite `state_file` with `positions`."""
    atomic_write_json(state_file, positions)


def add(state_file: str, position: dict) -> None:
    """Append one position to the book."""
    positions = load(state_file)
    positions.append(position)
    save(state_file, positions)


def remove(state_file: str, option_symbol: str) -> None:
    """Drop the position with this option_symbol from the book."""
    save(state_file, [p for p in load(state_file)
                      if p.get("option_symbol") != option_symbol])


def update(state_file: str, option_symbol: str, **fields) -> None:
    """Merge `fields` into the matching position in place (e.g. reduced quantity
    after a scale-out, or the running peak P&L for a trailing stop)."""
    positions = load(state_file)
    for p in positions:
        if p.get("option_symbol") == option_symbol:
            p.update(fields)
    save(state_file, positions)
