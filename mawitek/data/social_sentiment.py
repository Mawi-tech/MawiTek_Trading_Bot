"""
social_sentiment.py — per-ticker retail social sentiment (Stocktwits + Reddit).

The bot previously had no real social signal — "sentiment" was just keyword
counting on news headlines. This module adds two genuine retail-chatter sources
and turns them into a compact, sorted board the dashboard's Social tab renders:

    Stocktwits  — messages carry an explicit Bull/Bear tag; untagged ones fall
                  back to the keyword scorer. Gives a clean bull/bear ratio.
    Reddit      — r/wallstreetbets, r/stocks, r/investing search. No bull/bear
                  tag, so we use mention VOLUME + keyword sentiment on title+body.

Per ticker we emit:
    {ticker, stocktwits{…}, reddit{…}, sentiment_score(-1..1),
     net_sentiment(bullish|bearish|neutral), volume, updated}
sorted by `volume` (most-discussed first) into social_sentiment.json.

The AGGREGATORS (aggregate_stocktwits, aggregate_reddit, combine_social,
merge_social) are pure and unit-tested. The fetch_* functions are thin network
wrappers that fail soft — Stocktwits/Reddit rate-limit (403/429) freely, so a
blocked source just contributes nothing rather than breaking the sweep.

Run:
    python social_sentiment.py --once     # one sweep, then exit
    python social_sentiment.py NVDA AMD   # print sentiment for given tickers
"""

from __future__ import annotations

import argparse
import os

import requests

from mawitek.infra.logger import get_logger
from mawitek.infra.state_io import atomic_write_json, read_json, file_lock
from mawitek.infra.utils import now_est
from mawitek.data.news_catalyst import score_headline

log = get_logger("social_sentiment")

SOCIAL_FILE        = "social_sentiment.json"
SOCIAL_MAX_ITEMS   = 100      # rolling cap on stored tickers
MAX_SOCIAL_TICKERS = 20       # cap per sweep (social APIs rate-limit harder)
REDDIT_SUBS        = ["wallstreetbets", "stocks", "investing"]
NEUTRAL_BAND       = 0.15     # |score| below this is "neutral"

_UA = f"MawiTek Trading Bot/1.0 ({os.getenv('CONTACT_EMAIL', 'contact@example.com')})"
_SESSION = requests.Session()
_TIMEOUT = 10

ENABLE_STOCKTWITS = os.getenv("SOCIAL_STOCKTWITS", "1") != "0"
ENABLE_REDDIT     = os.getenv("SOCIAL_REDDIT",     "1") != "0"


# ══════════════════════════════════════════════════════════════════════════════
# Pure aggregators (unit-tested)
# ══════════════════════════════════════════════════════════════════════════════

def aggregate_stocktwits(messages: list[dict]) -> dict:
    """
    Summarize Stocktwits messages into a bull/bear breakdown.

    Each message may carry entities.sentiment.basic == "Bullish"/"Bearish".
    Untagged messages are keyword-scored so they still count. Returns:
        {bullish, bearish, neutral, messages, score(-1..1)}
    where score = (bull - bear) / (bull + bear).  Pure.
    """
    bullish = bearish = neutral = 0
    for m in messages or []:
        basic = (((m.get("entities") or {}).get("sentiment") or {}).get("basic"))
        if basic == "Bullish":
            bullish += 1
        elif basic == "Bearish":
            bearish += 1
        else:
            s = score_headline(m.get("body", "") or "")
            if s > 0:
                bullish += 1
            elif s < 0:
                bearish += 1
            else:
                neutral += 1

    total = bullish + bearish + neutral
    tagged = bullish + bearish
    score = round((bullish - bearish) / tagged, 3) if tagged else 0.0
    return {"bullish": bullish, "bearish": bearish, "neutral": neutral,
            "messages": total, "score": score}


def aggregate_reddit(posts: list[dict], ticker: str | None = None) -> dict:
    """
    Summarize Reddit search results: mention VOLUME + keyword sentiment.

    `posts` are Reddit listing children ({"data": {...}}) or bare data dicts.
    Returns {mentions, sentiment_sum, score(-1..1), upvotes, comments}. Pure.
    """
    mentions = sentiment_sum = upvotes = comments = 0
    for p in posts or []:
        d = p.get("data", p) if isinstance(p, dict) else {}
        title = d.get("title", "") or ""
        body = d.get("selftext", "") or ""
        mentions += 1
        sentiment_sum += score_headline(f"{title} {body}")
        upvotes += int(d.get("score", 0) or 0)
        comments += int(d.get("num_comments", 0) or 0)

    # Normalize summed keyword score into ~[-1, 1] (≈2 hits/post saturates).
    score = round(max(-1.0, min(1.0, sentiment_sum / (mentions * 2))), 3) if mentions else 0.0
    return {"mentions": mentions, "sentiment_sum": sentiment_sum, "score": score,
            "upvotes": upvotes, "comments": comments}


def combine_social(ticker: str, stocktwits: dict | None = None,
                   reddit: dict | None = None) -> dict:
    """
    Blend the two source summaries into one ticker verdict. The combined score
    is volume-weighted (a source with more chatter dominates). Pure.
    """
    st = stocktwits or {}
    rd = reddit or {}
    st_vol = st.get("messages", 0) or 0
    rd_vol = rd.get("mentions", 0) or 0

    parts = []
    if st_vol:
        parts.append((st.get("score", 0.0), st_vol))
    if rd_vol:
        parts.append((rd.get("score", 0.0), rd_vol))

    if parts:
        wsum = sum(w for _, w in parts)
        score = round(sum(s * w for s, w in parts) / wsum, 3) if wsum else 0.0
    else:
        score = 0.0

    net = ("bullish" if score >= NEUTRAL_BAND
           else "bearish" if score <= -NEUTRAL_BAND
           else "neutral")

    return {
        "ticker": str(ticker).upper(),
        "stocktwits": st or None,
        "reddit": rd or None,
        "sentiment_score": score,
        "net_sentiment": net,
        "volume": st_vol + rd_vol,
        "updated": now_est().isoformat(timespec="seconds"),
    }


def merge_social(existing: list[dict], new_items: list[dict],
                 max_items: int = SOCIAL_MAX_ITEMS) -> list[dict]:
    """Replace each ticker's entry with its fresh reading, keep the rest, sort by
    volume (most-discussed first), cap. Pure."""
    by_ticker = {i.get("ticker"): i for i in existing if i.get("ticker")}
    for it in new_items:
        t = it.get("ticker")
        if t:
            by_ticker[t] = it
    merged = sorted(by_ticker.values(), key=lambda i: i.get("volume", 0), reverse=True)
    return merged[:max_items]


# ══════════════════════════════════════════════════════════════════════════════
# Network fetchers (fail soft)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_stocktwits(ticker: str, max_messages: int = 30) -> list[dict]:
    """Raw Stocktwits messages for a symbol. [] on throttle/error."""
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = _SESSION.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        if r.status_code in (403, 429):
            log.debug("stocktwits throttled for %s (%d)", ticker, r.status_code)
            return []
        r.raise_for_status()
        return ((r.json() or {}).get("messages", []) or [])[:max_messages]
    except Exception as e:
        log.debug("stocktwits failed for %s: %s", ticker, e)
        return []


def fetch_reddit(ticker: str, limit: int = 15) -> list[dict]:
    """Recent Reddit posts mentioning the ticker across REDDIT_SUBS. Fails soft."""
    posts: list[dict] = []
    for sub in REDDIT_SUBS:
        try:
            url = f"https://www.reddit.com/r/{sub}/search.json"
            params = {"q": ticker, "restrict_sr": 1, "sort": "new",
                      "limit": limit, "t": "week"}
            r = _SESSION.get(url, headers={"User-Agent": _UA},
                             params=params, timeout=_TIMEOUT)
            if r.status_code in (403, 429):
                continue
            r.raise_for_status()
            children = (((r.json() or {}).get("data") or {}).get("children") or [])
            posts.extend(children)
        except Exception as e:
            log.debug("reddit r/%s failed for %s: %s", sub, ticker, e)
    return posts


def social_for_ticker(ticker: str) -> dict:
    """Full social verdict for one ticker (both sources, combined)."""
    st = aggregate_stocktwits(fetch_stocktwits(ticker)) if ENABLE_STOCKTWITS else {}
    rd = aggregate_reddit(fetch_reddit(ticker), ticker) if ENABLE_REDDIT else {}
    return combine_social(ticker, st, rd)


# ══════════════════════════════════════════════════════════════════════════════
# Sweep
# ══════════════════════════════════════════════════════════════════════════════

def social_sweep_once(tickers: list[str] | None = None) -> list[dict]:
    """
    One social pass: pull sentiment for up to MAX_SOCIAL_TICKERS, merge into
    SOCIAL_FILE. Returns the fresh per-ticker verdicts. Never raises.
    """
    if tickers is None:
        try:
            from mawitek.data.news_feed import _focus_tickers
            tickers = _focus_tickers()
        except Exception:
            tickers = []
    tickers = (tickers or [])[:MAX_SOCIAL_TICKERS]

    fresh: list[dict] = []
    for t in tickers:
        try:
            res = social_for_ticker(t)
            if res.get("volume", 0) > 0:      # only store tickers with chatter
                fresh.append(res)
        except Exception as e:
            log.debug("social sweep failed for %s: %s", t, e)

    if not fresh:
        return []

    try:
        with file_lock(SOCIAL_FILE):
            data = read_json(SOCIAL_FILE, [])
            existing = data if isinstance(data, list) else []
            merged = merge_social(existing, fresh)
            atomic_write_json(SOCIAL_FILE, merged)
    except Exception as e:
        log.warning("could not persist social feed: %s", e)
        return []

    log.info("social sweep: %d/%d ticker(s) with chatter", len(fresh), len(tickers))
    return fresh


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Per-ticker social sentiment.")
    parser.add_argument("--once", action="store_true", help="One sweep over the focus list, then exit.")
    parser.add_argument("tickers", nargs="*", help="Specific tickers to print.")
    args = parser.parse_args()

    if args.tickers:
        for t in args.tickers:
            r = social_for_ticker(t)
            print(f"{r['ticker']:6s} {r['net_sentiment']:8s} "
                  f"score={r['sentiment_score']:+.2f} vol={r['volume']}  "
                  f"st={r.get('stocktwits')} rd={r.get('reddit')}")
    elif args.once:
        fresh = social_sweep_once()
        print(f"Social sweep complete — {len(fresh)} ticker(s) with chatter.")
    else:
        feed = read_json(SOCIAL_FILE, [])
        if not isinstance(feed, list) or not feed:
            print("No social data yet. Run: python social_sentiment.py --once")
        else:
            for i in feed[:30]:
                print(f"{i.get('ticker',''):6s} {i.get('net_sentiment',''):8s} "
                      f"score={i.get('sentiment_score',0):+.2f} vol={i.get('volume',0)}")
