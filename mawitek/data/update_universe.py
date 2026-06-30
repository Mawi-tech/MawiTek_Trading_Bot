"""
update_universe.py  —  (re)generate the live scan universe CSVs.

The scanners call ``load_universe(csv_path=...)``. Without a CSV the bot SILENTLY
falls back to a 49-name hardcoded list (universe.DEFAULT_UNIVERSE), so it only
ever scans a fraction of the market. This script writes two universes:

  • sp500.csv          — the S&P 500 constituents (back-compat; tighter list)
  • market_universe.csv — the FULL US common-stock + ETF market, pulled from the
                          official Nasdaq Trader symbol directory (NASDAQ + NYSE
                          + NYSE American/Arca + others). This is what
                          ``universe.market_csv()`` prefers, so the live scanners
                          look at the WHOLE market, not just the S&P 500.

Membership drifts, so re-run periodically (e.g. weekly/monthly):

    python update_universe.py            # both CSVs (full market + S&P 500)
    python update_universe.py --sp500-only
    python update_universe.py --no-etfs  # exclude ETFs from the full universe

The full-market fetch reads the public symbol-directory files at
nasdaqtrader.com and filters out test issues plus non-common securities
(warrants / units / rights / preferreds / notes). The liquidity filter at scan
time (market_filter.filter_universe) then narrows this to names that actually
trade enough volume to be tradable, so the broad list is safe to keep wide.
"""

import csv
import io
import os
import urllib.request

from backtests.backtest_hft import fetch_sp500_tickers
from mawitek.data.universe import dedupe_symbols

_BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(_BASE, "sp500.csv")
MARKET_FILE = os.path.join(_BASE, "market_universe.csv")

# Official Nasdaq Trader symbol-directory files (pipe-delimited, free, no key).
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Security-name keywords that mark NON-common, usually non-optionable issues.
_EXCLUDE_NAME_KEYWORDS = (
    "warrant", "unit", "right", "preferred", "depositary", "depository",
    " notes", "debenture", "when issued", "when-issued", "convertible",
    "% ", "tender", "subscription", "redeemable",
)

# Symbol-suffix characters Nasdaq uses for non-common share classes:
#   $ preferred   ^ rights   + warrants   = / when-issued / class markers
_BAD_SYMBOL_CHARS = set("$^+=~")


def _read_pipe_file(url: str) -> list[dict]:
    """Download a pipe-delimited Nasdaq Trader directory file → list of row dicts."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    # The last line is a "File Creation Time: ..." footer — drop it.
    lines = [ln for ln in text.splitlines() if not ln.startswith("File Creation Time")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))


def _is_common(symbol: str, name: str, etf_flag: str, include_etfs: bool) -> bool:
    """True if this looks like a tradable common stock (or ETF, if allowed)."""
    if not symbol:
        return False
    s = symbol.strip().upper()
    if any(c in s for c in _BAD_SYMBOL_CHARS):
        return False
    nm = (name or "").lower()
    if any(k in nm for k in _EXCLUDE_NAME_KEYWORDS):
        return False
    if (etf_flag or "").strip().upper() == "Y" and not include_etfs:
        return False
    return True


def fetch_all_us_symbols(include_etfs: bool = True) -> list[str]:
    """
    Full US common-stock (+ ETF) universe from the Nasdaq Trader symbol directory.

    Combines nasdaqlisted.txt (Nasdaq) and otherlisted.txt (NYSE / NYSE American /
    Arca / etc). Filters out test issues and non-common securities. Falls back to
    the S&P 500 list on any network error or a suspiciously small result.
    """
    symbols: list[str] = []
    try:
        for row in _read_pipe_file(NASDAQ_LISTED_URL):
            if (row.get("Test Issue") or "").strip().upper() == "Y":
                continue
            sym = row.get("Symbol", "")
            if _is_common(sym, row.get("Security Name", ""), row.get("ETF", "N"), include_etfs):
                symbols.append(sym)

        for row in _read_pipe_file(OTHER_LISTED_URL):
            if (row.get("Test Issue") or "").strip().upper() == "Y":
                continue
            # otherlisted uses "ACT Symbol"; fall back to the NASDAQ symbol column.
            sym = row.get("ACT Symbol") or row.get("NASDAQ Symbol") or ""
            if _is_common(sym, row.get("Security Name", ""), row.get("ETF", "N"), include_etfs):
                symbols.append(sym)
    except Exception as e:
        print(f"[update_universe] full-market fetch failed: {e}; falling back to S&P 500.")
        return fetch_sp500_tickers()

    cleaned = dedupe_symbols(symbols)   # upper-cases, '.'→'-', drops junk rows
    if len(cleaned) < 500:
        print(f"[update_universe] full-market list suspiciously small "
              f"({len(cleaned)}); falling back to S&P 500.")
        return fetch_sp500_tickers()
    return cleaned


def _write_csv(symbols: list[str], output_file: str) -> int:
    with open(output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Symbol"])
        for t in symbols:
            writer.writerow([t])
    return len(symbols)


def update_universe(output_file: str = OUTPUT_FILE) -> int:
    """Fetch the S&P 500 list and write it to a one-column CSV. Returns count."""
    tickers = fetch_sp500_tickers()
    n = _write_csv(tickers, output_file)
    print(f"[update_universe] wrote {n} S&P 500 symbols -> {output_file}")
    return n


def build_market_universe(output_file: str = MARKET_FILE, include_etfs: bool = True) -> int:
    """Fetch the FULL US market and write it to market_universe.csv. Returns count."""
    tickers = fetch_all_us_symbols(include_etfs=include_etfs)
    n = _write_csv(tickers, output_file)
    print(f"[update_universe] wrote {n} full-market symbols -> {output_file}")
    return n


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate the scan-universe CSVs")
    parser.add_argument("--sp500-only", action="store_true",
                        help="Only regenerate sp500.csv (skip the full market list)")
    parser.add_argument("--no-etfs", action="store_true",
                        help="Exclude ETFs from the full-market universe")
    args = parser.parse_args()

    update_universe()
    if not args.sp500_only:
        build_market_universe(include_etfs=not args.no_etfs)
