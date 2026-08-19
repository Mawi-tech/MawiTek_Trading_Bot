"""
orphan_reconciler.py — adopt broker positions that no local book knows about.

WHY THIS EXISTS
---------------
Every strategy already reconciles one direction: `reconcile_*_positions()` drops
local book rows the broker no longer holds, journaling them `closed_externally`:

    stale = [p for p in local if p.get("option_symbol") not in broker_syms]

Nothing reconciles the OTHER direction. A position that exists at the broker but
in no local book is invisible to every exit manager — no take-profit, no stop,
no DTE close — and stays that way through expiry. Restarting the fleet does not
help, because the drop-only reconcilers never adopt.

This is not hypothetical. On 2026-08-18 the sandbox held eight such positions,
every local book empty, three days from expiry, one of them (BLDR 70P x9) in the
money and therefore headed for automatic exercise into a 900-share short.

Orphans arise routinely:
  * a fill confirmed during downtime (order_manager warns loudly, but cannot
    rebuild the entry metadata a book record needs)
  * a partial fill on a multi-leg structure, where some legs record and some do not
  * a book file lost, reset, or quarantined as corrupt by state_io
  * anything opened by hand in the broker UI

WHAT THIS DOES, AND DELIBERATELY DOES NOT DO
--------------------------------------------
Adopting is safe: it writes a book record so the position becomes visible, is
counted by the risk caps, and shows on the dashboard.

EXITING an orphan is NOT safe by default. The original entry intent is gone — we
do not know the take-profit, the stop, or the strategy that opened it, and
guessing wrong realises a loss the operator never chose. So:

  * detection, adoption and alerting are ALWAYS on
  * automated exits are OFF unless ENABLE_ORPHAN_AUTO_EXIT is set

With auto-exit off this module cannot place an order, so merging it can never
close a position by surprise. Turning it on enables ONE exit rule — a DTE floor
— which exists to prevent automatic exercise/assignment at expiry, the single
mechanical harm an unmanaged option position causes on its own.

Entry price is reconstructed from the broker's cost basis, which is exact:

    entry_price = abs(cost_basis) / (abs(quantity) * 100)

Everything else a book record normally carries (setup score, signals) is gone
and is recorded as absent, so a later closed-trade record stays honest.
"""

from __future__ import annotations

import datetime
import os
import re

from logger import get_logger
from state_io import read_json, atomic_write_json, file_lock
from utils import today_est, now_est

log = get_logger("orphan_reconciler")

# ─── Config ───────────────────────────────────────────────────────────────────

ORPHAN_BOOK = "adopted_positions.json"

# Automated exits for adopted positions. OFF by default — see the module
# docstring. Turning this on lets the DTE floor below place closing orders.
ENABLE_ORPHAN_AUTO_EXIT = os.getenv("ENABLE_ORPHAN_AUTO_EXIT", "").lower() == "true"

# The one exit rule adopted positions get, and only when auto-exit is enabled.
# An in-the-money long option left to expire is auto-exercised; a short one is
# assigned. Either converts a defined-risk option into an unwanted stock
# position — a long ITM put on 9 contracts becomes a 900-share short. Closing
# before expiry is a mechanical safety net, not a view on the trade.
ORPHAN_MIN_DTE_EXIT = 2

# Canonical registry of every position book. (strategy key, file, book is a list)
# The catalyst book is a DICT keyed by option symbol; the rest are lists.
# tests/test_orphan_reconciler.py asserts this matches risk_manager's own list,
# so adding a strategy cannot silently create a blind spot here.
BOOKS: list[tuple[str, str, bool]] = [
    ("catalyst_long_call", "open_positions.json",       False),
    ("iv_rank",            "iv_rank_positions.json",    True),
    ("hft_intraday",       "hft_positions.json",        True),
    ("pead",               "pead_positions.json",       True),
    ("bounce",             "bounce_positions.json",     True),
    ("vwap_fade",          "vwap_fade_positions.json",  True),
]

_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


# ─── OCC helpers ──────────────────────────────────────────────────────────────

def parse_occ(symbol: str) -> dict | None:
    """<UNDERLYING><YYMMDD><C|P><8-digit strike> to components, or None."""
    m = _OCC.match((symbol or "").strip().upper())
    if not m:
        return None
    und, yy, mm, dd, cp, strike = m.groups()
    return {
        "underlying": und,
        "expiration": f"20{yy}-{mm}-{dd}",
        "option_type": "call" if cp == "C" else "put",
        "strike": int(strike) / 1000.0,
    }


def dte_for(expiration: str, as_of: datetime.date | None = None) -> int | None:
    """Calendar days to expiry, anchored to the EASTERN trading day."""
    try:
        exp = datetime.date.fromisoformat(expiration)
    except (TypeError, ValueError):
        return None
    return (exp - (as_of or today_est())).days


# ─── Which symbols does a local book already own? ─────────────────────────────

def held_symbols(books: list[tuple[str, str, bool]] | None = None) -> dict[str, str]:
    """
    Map every option symbol any local book holds to that book's strategy key.

    Reads leniently: a book that is missing or unreadable contributes nothing.
    That is the correct failure direction — a book we cannot read must not cause
    its positions to be re-adopted as orphans and counted twice.
    """
    out: dict[str, str] = {}
    for strat, path, is_list in (books or BOOKS):
        if not os.path.exists(path):
            continue
        data = read_json(path, None)
        if data is None:
            log.warning("Book %s unreadable — treating its positions as still owned "
                        "rather than adopting them, to avoid double-counting", path)
            continue
        if isinstance(data, dict) and not is_list:
            for sym in data:                       # catalyst book: keyed BY symbol
                out[str(sym)] = strat
        elif isinstance(data, list):
            for rec in data:
                if not isinstance(rec, dict):
                    continue
                sym = rec.get("option_symbol") or rec.get("symbol")
                if sym:
                    out[str(sym)] = strat
                for leg in rec.get("legs", []) or []:      # iv_rank multi-leg
                    lsym = (leg or {}).get("option_symbol") or (leg or {}).get("symbol")
                    if lsym:
                        out[str(lsym)] = strat
    return out


# ─── Detection ────────────────────────────────────────────────────────────────

def find_orphans(broker_positions: list[dict],
                 books: list[tuple[str, str, bool]] | None = None) -> list[dict]:
    """
    Broker option positions that no local book claims and that are not already
    adopted. Equities and zero-quantity rows are skipped — this manages options.
    """
    owned = held_symbols(books)
    adopted = {r.get("option_symbol") for r in (read_json(ORPHAN_BOOK, []) or [])}
    orphans: list[dict] = []
    for pos in broker_positions or []:
        sym = str(pos.get("symbol", "") or "")
        qty = float(pos.get("quantity", 0) or 0)
        parsed = parse_occ(sym)
        if not parsed or qty == 0 or sym in owned or sym in adopted:
            continue
        cost = float(pos.get("cost_basis", 0) or 0)
        # Exact — the broker gives us the real basis. abs() on both sides so a
        # short leg (negative qty AND negative basis) yields a positive
        # per-contract price like every other book record.
        entry = abs(cost) / (abs(qty) * 100) if qty else 0.0
        orphans.append({
            "option_symbol": sym,
            "underlying":    parsed["underlying"],
            "expiration":    parsed["expiration"],
            "option_type":   parsed["option_type"],
            "strike":        parsed["strike"],
            "quantity":      int(qty),
            "side":          "long" if qty > 0 else "short",
            "entry_price":   round(entry, 4),
            "cost_basis":    round(cost, 2),
            "dte":           dte_for(parsed["expiration"]),
        })
    return orphans


# ─── Adoption ─────────────────────────────────────────────────────────────────

def adopt_orphans(broker_positions: list[dict],
                  books: list[tuple[str, str, bool]] | None = None,
                  notify: bool = True) -> list[dict]:
    """
    Record every orphan into ORPHAN_BOOK and alert once per symbol.

    Idempotent: find_orphans already excludes anything adopted, so repeated
    calls add nothing. Returns the newly adopted records.
    """
    new = find_orphans(broker_positions, books)
    if not new:
        return []

    stamp = now_est().isoformat(timespec="seconds")
    with file_lock(ORPHAN_BOOK):
        book = read_json(ORPHAN_BOOK, []) or []
        existing = {r.get("option_symbol") for r in book}
        for rec in new:
            if rec["option_symbol"] in existing:
                continue
            book.append({
                **rec,
                "strategy": "adopted",
                "adopted_at": stamp,
                "entry_time": stamp,
                # Honest about what could not be recovered: the entry intent is
                # gone, so a later closed-trade record must not imply a setup
                # score or signal snapshot it never had.
                "setup_score": None,
                "signals": {"origin": "orphan_adoption",
                            "note": "entry intent unrecoverable"},
            })
        atomic_write_json(ORPHAN_BOOK, book)

    for rec in new:
        log.warning("ADOPTED ORPHAN %s %s x%d @ $%.2f (DTE %s) — was at the broker "
                    "with no local book, so nothing was exit-managing it",
                    rec["side"], rec["option_symbol"], rec["quantity"],
                    rec["entry_price"], rec["dte"])
    if notify:
        _alert(new)
    return new


def _alert(records: list[dict]) -> None:
    """Best-effort operator alert. Never raises — adoption already succeeded."""
    try:
        from event_notifier import _dispatch
        lines = [f"{len(records)} broker position(s) had no local book and were not "
                 f"being exit-managed. They are now adopted and visible."]
        for r in records:
            near = ""
            if r.get("dte") is not None and r["dte"] <= ORPHAN_MIN_DTE_EXIT:
                near = "   <-- EXPIRES SOON"
            lines.append(f"{r['side']} {r['option_symbol']} x{r['quantity']} "
                         f"@ ${r['entry_price']:.2f}, DTE {r['dte']}{near}")
        if not ENABLE_ORPHAN_AUTO_EXIT:
            lines.append("Automated exits are OFF for adopted positions "
                         "(ENABLE_ORPHAN_AUTO_EXIT). Decide and act manually.")
        _dispatch(subject="Orphaned broker positions adopted",
                  lines=lines, severity="warning")
    except Exception as e:  # noqa: BLE001
        log.warning("could not send orphan alert: %s", e)


# ─── The one exit rule (opt-in) ───────────────────────────────────────────────

def orphan_exit_decision(position: dict, as_of: datetime.date | None = None) -> str | None:
    """
    Exit reason for an adopted position, or None to hold.

    Returns None unconditionally while ENABLE_ORPHAN_AUTO_EXIT is off, so this
    cannot place an order by default. When on, the only rule is the DTE floor:
    close before expiry so an in-the-money option is not auto-exercised or
    assigned into a stock position nobody chose.
    """
    if not ENABLE_ORPHAN_AUTO_EXIT:
        return None
    dte = dte_for(position.get("expiration", ""), as_of)
    if dte is not None and dte <= ORPHAN_MIN_DTE_EXIT:
        return "orphan_dte_floor"
    return None


# ─── Read-only report (CLI) ───────────────────────────────────────────────────

def report() -> list[dict]:
    """Print the current orphan set without writing or trading anything."""
    from tradier_client import get_open_positions
    orphans = find_orphans(get_open_positions())
    if not orphans:
        print("No orphaned broker positions — every position is in a local book.")
        return []
    print(f"{len(orphans)} orphaned broker position(s) — in NO local book, so "
          f"nothing is exit-managing them:\n")
    print(f"  {'SYMBOL':22} {'SIDE':6} {'QTY':>4} {'ENTRY':>9} {'DTE':>4}")
    for o in orphans:
        flag = ""
        if o["dte"] is not None and o["dte"] <= ORPHAN_MIN_DTE_EXIT:
            flag = "   <-- expiry risk"
        print(f"  {o['option_symbol']:22} {o['side']:6} {o['quantity']:4d} "
              f"{o['entry_price']:9.2f} {str(o['dte']):>4}{flag}")
    print(f"\nAuto-exit is {'ON' if ENABLE_ORPHAN_AUTO_EXIT else 'OFF'} "
          f"(ENABLE_ORPHAN_AUTO_EXIT). Run with --adopt to record them.")
    return orphans


if __name__ == "__main__":
    import sys
    if "--adopt" in sys.argv:
        from tradier_client import get_open_positions
        adopted = adopt_orphans(get_open_positions())
        print(f"Adopted {len(adopted)} orphan(s) into {ORPHAN_BOOK}.")
    else:
        report()
