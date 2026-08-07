"""
One-off, read-only diagnostic for the Overview-vs-Strategies-tab position
count mismatch (Overview showed ~8, IV-Rank tab showed 2).

Compares live broker option positions against iv_rank_positions.json to show
exactly which positions are missing from the IV-Rank book, and whether they
were ever recorded, dropped by reconcile_iv_positions(), or belong to another
strategy / were opened manually.

Run this in the live environment (needs real Tradier credentials — it makes
no changes, only reads):

    python3 diagnose_iv_rank_positions.py

Safe to delete after use; not wired into the bot or the test suite.
"""
from __future__ import annotations

import json

from tradier_client import get_open_positions
from dashboard_state import _symbol_strategy_map, _parse_occ_symbol
from iv_rank_bot import _load_iv_positions
from trade_journal import load_closed_trades


def main() -> None:
    broker_positions = get_open_positions(strict=True)
    broker_option_symbols = {
        p.get("symbol") for p in broker_positions
        if p.get("symbol") and len(p["symbol"]) > 6 and float(p.get("quantity", 0) or 0) != 0
    }

    iv_book = _load_iv_positions()
    iv_book_symbols: set[str] = set()
    for pos in iv_book:
        for leg in pos.get("legs", []):
            sym = leg.get("symbol")
            if sym:
                iv_book_symbols.add(sym)

    sym_map = _symbol_strategy_map()   # symbol -> strategy key, across ALL books

    print(f"Broker option positions (open, qty != 0): {len(broker_option_symbols)}")
    print(f"iv_rank_positions.json entries:            {len(iv_book)}")
    print(f"iv_rank_positions.json leg symbols:         {len(iv_book_symbols)}")
    print()

    # Broker symbols that IV-Rank's book doesn't know about, grouped by which
    # (if any) strategy book they DO belong to.
    missing_from_iv_book = sorted(broker_option_symbols - iv_book_symbols)
    print(f"Broker symbols NOT in iv_rank_positions.json: {len(missing_from_iv_book)}")
    for sym in missing_from_iv_book:
        parsed = _parse_occ_symbol(sym)
        underlying = parsed["underlying"] if parsed else "?"
        owner = sym_map.get(sym, "unattributed")
        print(f"  {sym:<24} underlying={underlying:<6} attributed_to={owner}")
    print()

    # iv_rank_positions.json entries whose legs are no longer at the broker —
    # these are candidates reconcile_iv_positions() should have dropped (or
    # will drop on next startup) as closed_externally.
    stale_book_entries = [
        pos for pos in iv_book
        if not ({l.get("symbol") for l in pos.get("legs", [])} & broker_option_symbols)
    ]
    print(f"iv_rank_positions.json entries with NO leg still at broker (stale): {len(stale_book_entries)}")
    for pos in stale_book_entries:
        print(f"  id={pos.get('id')} ticker={pos.get('ticker')} strategy={pos.get('strategy')} "
              f"legs={[l.get('symbol') for l in pos.get('legs', [])]}")
    print()

    # Cross-reference against the closed-trade journal: were any of the
    # "missing" symbols already journaled as closed_externally for iv_rank?
    closed = load_closed_trades()
    externally_closed_iv = {
        t.get("option_symbol") for t in closed
        if t.get("strategy") == "iv_rank" and t.get("exit_reason") == "closed_externally"
    }
    print(f"iv_rank trades journaled closed_externally in closed_trades.json: {len(externally_closed_iv)}")
    overlap = sorted(set(missing_from_iv_book) & externally_closed_iv)
    if overlap:
        print("  These 'missing' broker symbols WERE reconciled away previously:")
        for sym in overlap:
            print(f"    {sym}")
    else:
        print("  None of the currently-missing broker symbols match a closed_externally journal entry")
        print("  -> most likely explanation: these positions were never written to")
        print("     iv_rank_positions.json in the first place (e.g. a crash/exception")
        print("     between the broker fill and _save_iv_positions()), not reconciled away.")


if __name__ == "__main__":
    main()
