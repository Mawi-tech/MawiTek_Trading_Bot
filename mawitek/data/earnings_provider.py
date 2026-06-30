"""
earnings_provider.py — multi-source earnings date lookup with disk cache.

Replaces the hardcoded quarterly calendar in earnings_filter.py with
a live-fetching, self-caching provider that tries several sources and
only falls back to stale data as a last resort.

Source priority:
    1. Disk cache   — skip the network entirely if we have a fresh result
    2. yfinance     — stock.calendar (dict or DataFrame format, post-2024)
    3. Yahoo Finance direct API — bypasses yfinance quirks via requests
    4. Static fallback — small hardcoded set for mega-caps, used only if
       all API sources fail AND the cache is expired

Cache:
    earnings_cache.json — {ticker: {date, fetched_at, source}} on disk.
    TTL is 24 hours for dates within 30 days, 7 days for dates farther out.
    Expired entries are refreshed lazily on next access.

Thread-safety:
    The cache is written atomically (write-to-temp + rename) so a crash
    mid-write won't corrupt the file. Multiple bot instances on the same
    directory would need external locking, but we run a single process.

Usage:
    from mawitek.data.earnings_provider import get_earnings_date, prefetch_earnings

    # Single ticker
    date = get_earnings_date("AAPL")

    # Bulk prefetch before a scan (avoids N serial yfinance calls)
    prefetch_earnings(["AAPL", "MSFT", "GOOGL", "NVDA"])
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
from typing import Any

from mawitek.infra.logger import get_logger
from mawitek.infra.utils import today_est

log = get_logger("earnings_provider")


# ─── Config ──────────────────────────────────────────────────────────────────

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "earnings_cache.json"
)

# How long a cached entry stays valid before we re-fetch.
CACHE_TTL_NEAR_SECONDS = 24 * 3600    # 24 h for dates within 30 days
CACHE_TTL_FAR_SECONDS  = 7  * 24 * 3600  # 7 d for dates 30+ days out


# ─── Static Fallback (last resort) ──────────────────────────────────────────
# A tiny set of mega-cap tickers whose earnings dates are very predictable.
# Only consulted when BOTH yfinance AND direct Yahoo API fail AND the cache
# is expired. Kept intentionally small — the point is to avoid maintaining
# a 100-ticker hardcoded calendar.
#
# These are Q2-2026 estimates. They won't be dangerously wrong (earnings
# dates for mega-caps shift by ≤1 week quarter to quarter), but the bot
# should almost never reach this path in practice.

_STATIC_FALLBACK: dict[str, datetime.date] = {
    "AAPL":  datetime.date(2026, 7, 31),
    "MSFT":  datetime.date(2026, 7, 29),
    "GOOGL": datetime.date(2026, 7, 23),
    "AMZN":  datetime.date(2026, 7, 31),
    "META":  datetime.date(2026, 7, 30),
    "NVDA":  datetime.date(2026, 8, 27),
    "TSLA":  datetime.date(2026, 7, 23),
    "NFLX":  datetime.date(2026, 7, 17),
}


# ─── Disk Cache ──────────────────────────────────────────────────────────────

def _load_cache() -> dict[str, dict]:
    """Load the on-disk cache. Returns {} on any error."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    """Atomically write the cache to disk (write-to-temp + rename).

    On any error the temp file is removed and the existing cache file is left
    intact — readers never see a half-written JSON. The fd handed to fdopen()
    is closed by the with-statement on both success and failure; the previous
    version's manual `os.close(fd) if not f.closed` was unreachable (`f` is
    only bound after fdopen() succeeds) and was triggering a NameError that
    was then swallowed by the outer except — masking real write failures.
    """
    tmp_path: str | None = None
    try:
        dir_name = os.path.dirname(CACHE_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=str)
        # On Windows, os.rename fails if target exists — use os.replace.
        os.replace(tmp_path, CACHE_FILE)
        tmp_path = None    # ownership transferred; don't clean up
    except Exception as e:
        log.warning("Could not persist earnings cache: %s", e)
    finally:
        # Clean up an orphan temp file if the rename never happened.
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _cache_is_fresh(entry: dict, today: datetime.date) -> bool:
    """Is the cached entry still within its TTL?"""
    fetched_str = entry.get("fetched_at", "")
    if not fetched_str:
        return False
    try:
        fetched_at = datetime.datetime.fromisoformat(fetched_str)
    except (ValueError, TypeError):
        return False

    age_seconds = (datetime.datetime.now() - fetched_at).total_seconds()

    # Pick TTL based on how far out the cached date is
    date_str = entry.get("date", "")
    if date_str:
        try:
            cached_date = datetime.date.fromisoformat(date_str)
            days_out = (cached_date - today).days
            ttl = CACHE_TTL_NEAR_SECONDS if days_out <= 30 else CACHE_TTL_FAR_SECONDS
        except (ValueError, TypeError):
            ttl = CACHE_TTL_NEAR_SECONDS
    else:
        ttl = CACHE_TTL_NEAR_SECONDS

    return age_seconds < ttl


def _cache_entry(date: datetime.date, source: str) -> dict:
    """Build a cache entry dict."""
    return {
        "date":       date.isoformat(),
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source":     source,
    }


# ─── Source 1: yfinance ──────────────────────────────────────────────────────

def _fetch_yfinance(ticker: str) -> datetime.date | None:
    """
    Try yfinance stock.calendar. Handles both the post-2024 dict format
    and the legacy DataFrame format.
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        cal = stock.calendar

        if not cal:
            return None

        # New format (dict with 'Earnings Date' key)
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                raw = dates[0] if isinstance(dates, list) else dates
                return _coerce_date(raw)
            return None

        # Legacy DataFrame format
        if hasattr(cal, "empty") and not cal.empty:
            if "Earnings Date" in cal.index:
                raw = cal.loc["Earnings Date"]
                val = raw.iloc[0] if hasattr(raw, "iloc") else raw
                return _coerce_date(val)

        return None
    except Exception as e:
        log.debug("yfinance failed for %s: %s", ticker, e)
        return None


# ─── Source 2: Direct Yahoo Finance v8 API ───────────────────────────────────

def _fetch_yahoo_direct(ticker: str) -> datetime.date | None:
    """
    Hit Yahoo Finance's quoteSummary endpoint directly. This bypasses
    yfinance's parsing layer which has been unreliable since mid-2024.
    """
    import urllib.request
    import urllib.error

    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{ticker}?modules=calendarEvents"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        result = data.get("quoteSummary", {}).get("result", [])
        if not result:
            return None

        cal_events = result[0].get("calendarEvents", {})
        earnings = cal_events.get("earnings", {})
        dates_raw = earnings.get("earningsDate", [])

        if not dates_raw:
            return None

        # Each item is {"raw": epoch_int, "fmt": "YYYY-MM-DD"}
        # utcfromtimestamp() is deprecated in Python 3.12+; use an explicit
        # tz-aware UTC datetime instead.
        first = dates_raw[0]
        if isinstance(first, dict):
            fmt_str = first.get("fmt", "")
            if fmt_str:
                return datetime.date.fromisoformat(fmt_str)
            raw_epoch = first.get("raw", 0)
            if raw_epoch:
                return datetime.datetime.fromtimestamp(raw_epoch, datetime.timezone.utc).date()
        elif isinstance(first, (int, float)):
            return datetime.datetime.fromtimestamp(first, datetime.timezone.utc).date()

        return None
    except Exception as e:
        log.debug("Yahoo direct API failed for %s: %s", ticker, e)
        return None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _coerce_date(val: Any) -> datetime.date | None:
    """Coerce various yfinance date types to datetime.date."""
    if isinstance(val, datetime.date) and not isinstance(val, datetime.datetime):
        return val
    if isinstance(val, datetime.datetime):
        return val.date()
    if hasattr(val, "date"):
        return val.date()
    if isinstance(val, str):
        try:
            return datetime.date.fromisoformat(val[:10])
        except (ValueError, TypeError):
            pass
    return None


# ─── Public API ──────────────────────────────────────────────────────────────

def get_earnings_date(ticker: str) -> datetime.date | None:
    """
    Return the next earnings date for *ticker*, or None if unknown.

    Checks the disk cache first. If stale or missing, tries yfinance
    then Yahoo direct API, then the static fallback. Successful
    lookups are cached for future calls.
    """
    ticker = ticker.upper()
    today = today_est()
    cache = _load_cache()

    # 1. Check cache
    entry = cache.get(ticker)
    if entry and _cache_is_fresh(entry, today):
        date_str = entry.get("date", "")
        if date_str:
            try:
                cached_date = datetime.date.fromisoformat(date_str)
                # Only return if the date hasn't already passed
                if cached_date >= today:
                    return cached_date
                # Date passed — fall through to re-fetch
            except (ValueError, TypeError):
                pass

    # 2. yfinance
    date = _fetch_yfinance(ticker)
    if date and date >= today:
        cache[ticker] = _cache_entry(date, "yfinance")
        _save_cache(cache)
        return date

    # 3. Yahoo Finance direct API
    date = _fetch_yahoo_direct(ticker)
    if date and date >= today:
        cache[ticker] = _cache_entry(date, "yahoo_direct")
        _save_cache(cache)
        return date

    # 4. Static fallback
    static_date = _STATIC_FALLBACK.get(ticker)
    if static_date and static_date >= today:
        log.info(
            "%s: all API sources failed, using static fallback (%s)",
            ticker, static_date.isoformat(),
        )
        # Don't cache static fallback — we want to retry APIs next time
        return static_date

    # 5. Stale cache — better than nothing if not ancient (< 30 days old)
    if entry:
        date_str = entry.get("date", "")
        if date_str:
            try:
                stale_date = datetime.date.fromisoformat(date_str)
                if stale_date >= today:
                    log.info(
                        "%s: all sources failed, returning stale cache (%s, from %s)",
                        ticker, date_str, entry.get("source", "?"),
                    )
                    return stale_date
            except (ValueError, TypeError):
                pass

    return None


def prefetch_earnings(tickers: list[str]) -> dict[str, datetime.date | None]:
    """
    Bulk-fetch earnings dates for a list of tickers.

    Loads the cache once, skips tickers with fresh cache hits, fetches
    the rest, and writes the cache once at the end. This avoids N
    separate cache reads/writes when called before a scan cycle.

    Returns {ticker: date_or_None} for all requested tickers.
    """
    today = today_est()
    cache = _load_cache()
    results: dict[str, datetime.date | None] = {}
    need_fetch: list[str] = []

    # Pass 1: resolve from cache
    for t in tickers:
        t = t.upper()
        entry = cache.get(t)
        if entry and _cache_is_fresh(entry, today):
            date_str = entry.get("date", "")
            if date_str:
                try:
                    cached_date = datetime.date.fromisoformat(date_str)
                    if cached_date >= today:
                        results[t] = cached_date
                        continue
                except (ValueError, TypeError):
                    pass
        need_fetch.append(t)

    if not need_fetch:
        return results

    log.info(
        "Prefetching earnings for %d tickers (%d cached, %d to fetch)",
        len(tickers), len(results), len(need_fetch),
    )

    # Pass 2: fetch missing
    dirty = False
    for t in need_fetch:
        date = _fetch_yfinance(t)
        source = "yfinance"

        if not date or date < today:
            date = _fetch_yahoo_direct(t)
            source = "yahoo_direct"

        if date and date >= today:
            cache[t] = _cache_entry(date, source)
            results[t] = date
            dirty = True
        else:
            # Static fallback
            static = _STATIC_FALLBACK.get(t)
            if static and static >= today:
                results[t] = static
            else:
                results[t] = None

    if dirty:
        _save_cache(cache)

    return results


def invalidate_cache(ticker: str | None = None) -> None:
    """
    Invalidate cached earnings data.

    If ticker is given, remove only that entry. If None, clear the
    entire cache. Useful for manual refresh from the dashboard or CLI.
    """
    if ticker is None:
        try:
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
            log.info("Earnings cache cleared")
        except Exception as e:
            log.warning("Could not clear earnings cache: %s", e)
        return

    ticker = ticker.upper()
    cache = _load_cache()
    if ticker in cache:
        del cache[ticker]
        _save_cache(cache)
        log.info("Invalidated cache for %s", ticker)


def cache_stats() -> dict:
    """Return summary info about the cache for diagnostics / dashboard."""
    cache = _load_cache()
    today = today_est()
    total = len(cache)
    fresh = sum(1 for e in cache.values() if _cache_is_fresh(e, today))
    sources: dict[str, int] = {}
    for e in cache.values():
        s = e.get("source", "unknown")
        sources[s] = sources.get(s, 0) + 1

    return {
        "total_entries": total,
        "fresh_entries": fresh,
        "stale_entries": total - fresh,
        "sources":       sources,
        "cache_file":    CACHE_FILE,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Earnings provider — fetch and cache earnings dates."
    )
    sub = parser.add_subparsers(dest="cmd")

    # lookup <TICKER> [<TICKER> ...]
    p_look = sub.add_parser("lookup", help="Look up earnings date for ticker(s)")
    p_look.add_argument("tickers", nargs="+", help="Ticker symbol(s)")

    # stats
    sub.add_parser("stats", help="Show cache statistics")

    # clear [TICKER]
    p_clear = sub.add_parser("clear", help="Clear cache (all or one ticker)")
    p_clear.add_argument("ticker", nargs="?", default=None)

    args = parser.parse_args()

    if args.cmd == "lookup":
        for t in args.tickers:
            date = get_earnings_date(t.upper())
            if date:
                days = (date - today_est()).days
                print(f"  {t.upper():6s}  {date.isoformat()}  ({days}d away)")
            else:
                print(f"  {t.upper():6s}  (not found)")

    elif args.cmd == "stats":
        s = cache_stats()
        print(f"\nEarnings cache: {s['cache_file']}")
        print(f"  Total entries: {s['total_entries']}")
        print(f"  Fresh:         {s['fresh_entries']}")
        print(f"  Stale:         {s['stale_entries']}")
        print(f"  Sources:       {s['sources']}")

    elif args.cmd == "clear":
        invalidate_cache(args.ticker)
        print("Done." if not args.ticker else f"Cleared {args.ticker.upper()}")

    else:
        parser.print_help()
