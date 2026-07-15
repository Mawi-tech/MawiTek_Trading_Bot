"""
utils.py — small shared helpers.

Why the timezone helpers exist:
    The bot trades US options on Tradier. "Today" and "now" both have to
    mean US/Eastern, not whatever the host machine's locale happens to be.
    Otherwise a server in UTC would roll over to a new trading day 4–5
    hours before the market does, blowing up the daily-loss state reset
    and any DTE math that says "expires in N days".

Use:
    from utils import now_est, today_est
    if some_date == today_est():
        ...
"""

import datetime
from zoneinfo import ZoneInfo

# Single source of truth for market timezone. zoneinfo handles EST↔EDT
# automatically — no DST math here.
EASTERN = ZoneInfo("America/New_York")


def now_est() -> datetime.datetime:
    """Current wall-clock time in US/Eastern."""
    return datetime.datetime.now(EASTERN)


def today_est() -> datetime.date:
    """Today's date in US/Eastern. Use this everywhere instead of
    datetime.date.today() so the bot agrees with the market on what
    day it is, regardless of where the server runs."""
    return now_est().date()


def parse_isodt(s: str) -> datetime.datetime:
    """
    Parse an ISO-8601 datetime string and return a timezone-aware value.

    Old records (and a few helpers that pre-date the now_est() rollout) wrote
    naive timestamps in the host's local clock; new records write tz-aware
    US/Eastern times. To keep arithmetic against now_est() crash-free during
    the transition we treat naive strings as US/Eastern.
    """
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt


def percent_change(entry, current):
    return (current - entry) / entry


def spread_pct(bid: float, ask: float) -> float:
    """
    Bid/ask spread as a fraction of the mid price: (ask - bid) / mid.

    Returns 1.0 (treat as maximally wide / untradeable) when the mid is non-
    positive, e.g. a missing or crossed quote. Used by every executor's
    contract-selection liquidity filter, so it lives here as one definition.
    """
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else 1.0


def is_market_open(open_h: int, open_m: int, close_h: int, close_m: int,
                   now: datetime.datetime | None = None) -> bool:
    """
    True if `now` (default: now_est()) is a weekday within the [open, close]
    window, with the window bounds given in US/Eastern. PURE — clock math only;
    it does NOT know about exchange holidays (see is_market_session_open).

    Each strategy passes its own window (intraday strategies close earlier than
    swing strategies), so the bounds are parameters rather than constants here.
    """
    now = now or now_est()
    if now.weekday() >= 5:                      # 5=Sat, 6=Sun
        return False
    open_t  = now.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
    close_t = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_t <= now <= close_t


def is_market_session_open(open_h: int, open_m: int, close_h: int, close_m: int,
                           now: datetime.datetime | None = None) -> bool:
    """
    is_market_open() AND the broker's live market clock.

    The pure weekday+window check can't see exchange holidays or half-days
    (July 4th, the 1pm close before Thanksgiving), so the trading loops used to
    scan stale quotes and push orders into a closed market on those days. This
    additionally asks Tradier's /markets/clock (TTL-cached in tradier_client,
    ~one read per 5 min per process) whether the session is really open.

    Fails OPEN: if the clock can't be read (MOCK_MODE, network error) the local
    window check alone decides — behavior is then identical to is_market_open,
    so a data problem never stops trading on a normal day.
    """
    if not is_market_open(open_h, open_m, close_h, close_m, now):
        return False
    try:
        from tradier_client import market_open_now
        per_broker = market_open_now()
    except Exception:
        return True
    return True if per_broker is None else per_broker
