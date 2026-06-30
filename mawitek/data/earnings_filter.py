"""
earnings_filter.py

Fetches upcoming earnings dates and flags tickers with earnings within
a configurable window (default 1-5 days).

Delegates all date lookups to earnings_provider, which handles:
    1. Disk cache (skip the network if we already know)
    2. yfinance stock.calendar
    3. Yahoo Finance direct API (v10 quoteSummary)
    4. Tiny static fallback for mega-caps

This module stays thin — it only adds the "filter a list of tickers"
logic and the CLI preview on top of earnings_provider.get_earnings_date().
"""

from mawitek.data.earnings_provider import get_earnings_date, prefetch_earnings, cache_stats
from mawitek.infra.utils import today_est


# ─── Date Lookup ──────────────────────────────────────────────────────────────

def days_until_earnings(ticker: str) -> int | None:
    """Days from today (US/Eastern) until the next earnings date, or None."""
    earnings_date = get_earnings_date(ticker)
    if earnings_date is None:
        return None
    return (earnings_date - today_est()).days


def has_earnings_catalyst(ticker: str, min_days: int = 1, max_days: int = 5) -> bool:
    """True when earnings are within [min_days, max_days] from today."""
    days = days_until_earnings(ticker)
    if days is None:
        return False
    result = min_days <= days <= max_days
    print(
        f"[Earnings] {ticker} | Days until earnings: {days} | "
        f"In window ({min_days}-{max_days}d): {result}"
    )
    return result


def filter_by_earnings(
    symbols: list[str],
    min_days: int = 1,
    max_days: int = 5,
) -> list[dict]:
    """
    Return [{ticker, days_until_earnings}, ...] sorted by soonest first.

    Uses bulk prefetch to avoid N serial API calls.
    """
    today = today_est()

    # Prefetch all tickers at once (cache-aware, single disk write)
    date_map = prefetch_earnings(symbols)

    results = []
    for ticker in symbols:
        t = ticker.upper()
        edate = date_map.get(t)
        if edate is None:
            continue
        days = (edate - today).days
        if min_days <= days <= max_days:
            results.append({"ticker": t, "days_until_earnings": days})

    results.sort(key=lambda x: x["days_until_earnings"])
    return results


# ─── CLI Preview ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Preview upcoming earnings for a set of tickers."
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Lookahead window in days (default 14).",
    )
    parser.add_argument(
        "--tickers", nargs="*", default=None,
        help="Specific tickers to check. If omitted, checks a built-in watchlist.",
    )
    args = parser.parse_args()

    # Default watchlist if none given
    watchlist = args.tickers or [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX",
        "AMD", "INTC", "AVGO", "CRM", "ADBE", "PYPL", "UBER", "SHOP",
        "COIN", "PLTR", "JPM", "BAC", "GS", "XOM", "LLY", "UNH",
    ]
    watchlist = [t.upper() for t in watchlist]

    today = today_est()
    print(f"\n=== Earnings calendar — next {args.days} days (as of {today.isoformat()}) ===\n")

    date_map = prefetch_earnings(watchlist)

    upcoming = []
    for ticker in watchlist:
        edate = date_map.get(ticker)
        if edate is None:
            continue
        delta = (edate - today).days
        if 0 <= delta <= args.days:
            upcoming.append((ticker, edate, delta))

    upcoming.sort(key=lambda x: x[2])

    if not upcoming:
        print(f"  (no earnings in next {args.days} days)")
    else:
        for ticker, edate, delta in upcoming:
            print(f"  {ticker:6s}  {edate.isoformat()}  ({delta}d)")

    print(f"\n  Tickers checked: {len(watchlist)}")
    stats = cache_stats()
    print(f"  Cache: {stats['total_entries']} entries ({stats['fresh_entries']} fresh)")
    print(f"  Sources: {stats['sources']}\n")
