"""
earnings_filter.py

Fetches upcoming earnings dates and flags tickers with earnings within
a configurable window (default 1-5 days).

Primary source: yfinance (handles new dict format, post-2024 API change).
Fallback: hardcoded calendar for major tickers — yfinance.earnings_dates
has been broken since mid-2024, so the hardcoded calendar fills the gap
when stock.calendar also returns nothing.

UPDATE THE HARDCODED CALENDAR QUARTERLY. Last reviewed: April 2026
for Q2 2026 reporting. Dates are best-effort estimates — verify before
trading on them.
"""

import datetime
import yfinance as yf


# ─── Hardcoded Earnings Calendar ────────────────────────────────────────────────
# Used as a fallback when yfinance returns no calendar data.

HARDCODED_EARNINGS: dict[str, datetime.date] = {
    # Mega-cap tech
    "AAPL":  datetime.date(2026, 7, 31),
    "MSFT":  datetime.date(2026, 7, 29),
    "GOOGL": datetime.date(2026, 7, 23),
    "AMZN":  datetime.date(2026, 7, 31),
    "META":  datetime.date(2026, 7, 30),
    "NVDA":  datetime.date(2026, 8, 27),
    "TSLA":  datetime.date(2026, 7, 23),
    "NFLX":  datetime.date(2026, 7, 17),

    # Semis
    "AMD":   datetime.date(2026, 8,  5),
    "INTC":  datetime.date(2026, 7, 24),
    "AVGO":  datetime.date(2026, 9,  4),
    "MU":    datetime.date(2026, 6, 25),
    "QCOM":  datetime.date(2026, 7, 30),
    "ARM":   datetime.date(2026, 8,  6),
    "MRVL":  datetime.date(2026, 8, 28),
    "AMAT":  datetime.date(2026, 8, 14),
    "KLAC":  datetime.date(2026, 7, 24),
    "LRCX":  datetime.date(2026, 7, 23),
    "SMCI":  datetime.date(2026, 8,  5),

    # SaaS / cloud
    "ADBE":  datetime.date(2026, 6, 17),
    "CRM":   datetime.date(2026, 8, 27),
    "SNOW":  datetime.date(2026, 8, 21),
    "DDOG":  datetime.date(2026, 8,  7),
    "NET":   datetime.date(2026, 8,  7),
    "ZS":    datetime.date(2026, 9,  3),
    "PANW":  datetime.date(2026, 8, 18),
    "CRWD":  datetime.date(2026, 8, 25),
    "ANET":  datetime.date(2026, 7, 30),

    # Consumer / fintech
    "PYPL":  datetime.date(2026, 7, 29),
    "UBER":  datetime.date(2026, 8,  5),
    "SHOP":  datetime.date(2026, 8,  6),
    "HOOD":  datetime.date(2026, 8,  6),
    "RBLX":  datetime.date(2026, 8,  7),
    "PLTR":  datetime.date(2026, 8,  4),
    "MSTR":  datetime.date(2026, 7, 30),
    "COIN":  datetime.date(2026, 7, 30),
    "TTD":   datetime.date(2026, 8,  7),
    "ABNB":  datetime.date(2026, 8,  6),

    # Retail / staples
    "COST":  datetime.date(2026, 9, 25),
    "WMT":   datetime.date(2026, 8, 21),

    # Financials
    "JPM":   datetime.date(2026, 7, 14),
    "BAC":   datetime.date(2026, 7, 16),
    "GS":    datetime.date(2026, 7, 15),

    # Energy
    "XOM":   datetime.date(2026, 8,  1),
    "CVX":   datetime.date(2026, 8,  1),

    # Healthcare
    "LLY":   datetime.date(2026, 8,  7),
    "UNH":   datetime.date(2026, 7, 16),
    "JNJ":   datetime.date(2026, 7, 17),
    "PFE":   datetime.date(2026, 8,  5),
}


# ─── Date Lookup ────────────────────────────────────────────────────────────────

def _parse_yf_calendar(calendar) -> datetime.date | None:
    """Pull the next earnings date from a yfinance calendar object,
    handling both the new dict format (post-2024) and the legacy DataFrame."""

    if not calendar:
        return None

    # New format (post-2024 yfinance): dict with 'Earnings Date' key
    if isinstance(calendar, dict):
        dates = calendar.get("Earnings Date", [])
        if dates:
            raw = dates[0] if isinstance(dates, list) else dates
            if isinstance(raw, datetime.datetime):
                return raw.date()
            if isinstance(raw, datetime.date):
                return raw
            if hasattr(raw, "date"):
                return raw.date()
        return None

    # Legacy format (pre-2024): DataFrame
    if hasattr(calendar, "empty") and not calendar.empty:
        if "Earnings Date" in calendar.index:
            raw = calendar.loc["Earnings Date"]
            date_val = raw.iloc[0] if hasattr(raw, "iloc") else raw
            if hasattr(date_val, "date"):
                return date_val.date()

    return None


def get_earnings_date(ticker: str) -> datetime.date | None:
    """
    Pull the next reported earnings date for a ticker.

    Tries yfinance first. Falls back to hardcoded calendar if yfinance
    returns nothing or errors out. Returns None if neither source has data.
    """
    # Try yfinance
    try:
        stock = yf.Ticker(ticker)
        result = _parse_yf_calendar(stock.calendar)
        if result is not None:
            return result
    except Exception:
        # Silent fallthrough — no need to spam the console for every ticker
        pass

    # Fall back to hardcoded calendar
    return HARDCODED_EARNINGS.get(ticker.upper())


def days_until_earnings(ticker: str) -> int | None:
    earnings_date = get_earnings_date(ticker)
    if earnings_date is None:
        return None
    return (earnings_date - datetime.date.today()).days


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
    """Return [{ticker, days_until_earnings}, ...] sorted by soonest first."""
    results = []
    for ticker in symbols:
        days = days_until_earnings(ticker)
        if days is not None and min_days <= days <= max_days:
            results.append({"ticker": ticker, "days_until_earnings": days})
    results.sort(key=lambda x: x["days_until_earnings"])
    return results


# ─── CLI Preview ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Preview the hardcoded earnings calendar.")
    parser.add_argument("--days", type=int, default=14,
                        help="Lookahead window in days (default 14).")
    args = parser.parse_args()

    today = datetime.date.today()
    print(f"\n=== Earnings calendar — next {args.days} days (as of {today.isoformat()}) ===\n")

    upcoming = []
    for ticker, date in HARDCODED_EARNINGS.items():
        delta = (date - today).days
        if 0 <= delta <= args.days:
            upcoming.append((ticker, date, delta))

    upcoming.sort(key=lambda x: x[2])

    if not upcoming:
        print(f"  (no earnings in next {args.days} days from hardcoded calendar)")
    else:
        for ticker, date, delta in upcoming:
            print(f"  {ticker:6s}  {date.isoformat()}  ({delta}d)")

    print(f"\n  Total in calendar: {len(HARDCODED_EARNINGS)} tickers\n")
