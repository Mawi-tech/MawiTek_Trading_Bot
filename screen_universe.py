"""
screen_universe.py  —  build liquid_universe.csv (cut illiquid + micro-caps).

The full-market list (market_universe.csv, ~10k names from update_universe.py)
contains a long tail of names the bot can't actually trade: illiquid stocks,
micro-caps, and names with no real options. Rotating the live scanners through
all 10k wastes ~80-90% of every cycle re-rejecting junk and makes a full-market
sweep take hours.

This script screens the full market ONCE down to the tradable, non-micro-cap
names — price >= $5, >= 1M avg daily shares, >= $20M avg daily dollar volume —
and writes the survivors to liquid_universe.csv, which the live scanners rotate
through (universe.scan_csv()). Roughly ~1,000-1,200 names survive.

Liquidity is stable week to week, so this is a once-a-week batch job — run it
alongside update_universe.py. A cold run downloads daily history per name (~45-60
min for the full market); it reuses the shared daily liquidity cache, so a
same-day re-run is fast and an interrupted run resumes cheaply. The per-scan
liquidity filter still re-validates each name live, so a slightly stale liquid
list is safe.

    python screen_universe.py
    python screen_universe.py --min-dollar-volume 30000000   # stricter micro-cap cut
"""

import csv
import os

from universe import load_universe, market_csv, dedupe_symbols
from market_filter import filter_universe

_BASE = os.path.dirname(os.path.abspath(__file__))
LIQUID_FILE = os.path.join(_BASE, "liquid_universe.csv")

# "Tradable, non-micro-cap" floor — this is what cuts the illiquid tail + micro-caps.
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
MIN_DOLLAR_VOLUME = 20_000_000

# Screen in chunks so progress prints and the shared liquidity cache persists
# incrementally (an interrupted run resumes from the cache on the next attempt).
CHUNK = 200


def screen_universe(
    min_price: float = MIN_PRICE,
    min_avg_volume: int = MIN_AVG_VOLUME,
    min_dollar_volume: int = MIN_DOLLAR_VOLUME,
    output_file: str = LIQUID_FILE,
) -> int:
    """Screen the full market down to liquid names and write liquid_universe.csv."""
    symbols = load_universe(csv_path=market_csv(), limit=None)
    total = len(symbols)
    print(f"[screen_universe] screening {total} symbols "
          f"(>= ${min_price:g}, >= {min_avg_volume:,} sh/day, >= ${min_dollar_volume:,}/day)...")

    liquid: list[str] = []
    for i in range(0, total, CHUNK):
        chunk = symbols[i:i + CHUNK]
        liquid.extend(filter_universe(
            chunk,
            min_price=min_price,
            min_avg_volume=min_avg_volume,
            min_avg_dollar_volume=min_dollar_volume,
        ))
        print(f"[screen_universe] {min(i + CHUNK, total)}/{total} screened "
              f"-> {len(liquid)} liquid so far")

    liquid = dedupe_symbols(liquid)
    with open(output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Symbol"])
        for t in liquid:
            writer.writerow([t])

    pct = (len(liquid) / total * 100) if total else 0
    print(f"[screen_universe] wrote {len(liquid)} liquid symbols "
          f"({pct:.0f}% of {total}) -> {output_file}")
    return len(liquid)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Build liquid_universe.csv — cut illiquid tickers + micro-caps")
    parser.add_argument("--min-price", type=float, default=MIN_PRICE)
    parser.add_argument("--min-volume", type=int, default=MIN_AVG_VOLUME,
                        help="Minimum average daily share volume")
    parser.add_argument("--min-dollar-volume", type=int, default=MIN_DOLLAR_VOLUME,
                        help="Minimum average daily dollar volume (the micro-cap cut)")
    args = parser.parse_args()

    screen_universe(
        min_price=args.min_price,
        min_avg_volume=args.min_volume,
        min_dollar_volume=args.min_dollar_volume,
    )
