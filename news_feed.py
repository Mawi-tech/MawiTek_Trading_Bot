"""
news_feed.py — fast, categorized headline feed for the dashboard News tab.

The strategies already use news as a *score input* (news_catalyst). This module
is different: it keeps a human-readable, continuously-updated feed of WHAT is
happening — product releases, hiring/firing, M&A, regulatory actions, analyst
moves — so the operator can react to news without leaving the dashboard.

How it stays fast:
    A dedicated monitor process (started by start_all.py) polls every
    POLL_SECONDS for a focused ticker list: every held position, every ticker
    currently on the scanner-setups list, plus a core watchlist. New headlines
    are categorized, scored for sentiment/impact, merged (deduped) into
    news_feed.json, and HIGH-impact items push a notification to subscribers.
    The dashboard's News tab fetches news_feed.json directly on its own poll,
    so a headline shows up within one poll cycle of being published upstream.

Sources (no extra API keys — see news_sources.py, all AGGREGATED + deduped):
    1. Tradier /markets/news   (production accounts; the sandbox 404s it)
    2. Google News RSS         (aggregates thousands of outlets per ticker)
    3. yfinance ticker news    (works everywhere, minutes-level lag)
    4. SEC EDGAR 8-K filings   (official material-event filings)
    True real-time (sub-second) news needs a paid feed (Benzinga, UW news);
    news_sources.py is the single seam to plug one in later.

Each sweep also kicks a SOCIAL-sentiment pass (Stocktwits + Reddit, every
SOCIAL_POLL_EVERY cycles) via social_sentiment.py → social_sentiment.json,
which the dashboard's Social tab renders separately from the headline feed.

Run:
    python news_feed.py --monitor    # the polling loop (what start_all runs)
    python news_feed.py --once       # one sweep, then exit
    python news_feed.py              # print the current feed
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import time

from logger import get_logger
from state_io import atomic_write_json, read_json, file_lock
from utils import now_est
from news_catalyst import score_headline

log = get_logger("news_feed")

NEWS_FEED_FILE   = "news_feed.json"
NEWS_MAX_ITEMS   = 200      # rolling cap on the stored feed
POLL_SECONDS     = 60       # sweep cadence for the monitor loop
MAX_FOCUS_TICKERS = 40      # cap the per-sweep ticker list (keeps a sweep ~1 min)
MAX_HEADLINES_PER_TICKER = 8
SOCIAL_POLL_EVERY = 3       # run a social-sentiment sweep every Nth news sweep (~3 min)

# Always-watched liquid leaders — news here moves the whole tape.
CORE_WATCHLIST = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META",
                  "GOOGL", "TSLA", "AMD", "NFLX", "AVGO"]


# ─── Categorization (pure — unit-tested) ──────────────────────────────────────
# First matching category wins, so order encodes priority: specific,
# market-moving categories before generic ones ("approval" should hit
# regulatory before product; "deal" should hit M&A before partnership).

CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("ma",          ["acquire", "acquisition", "merger", "buyout", "takeover",
                     "to buy ", "stake in", "tender offer", "goes private"]),
    ("regulatory",  ["fda", "sec ", "antitrust", "probe", "investigation",
                     "lawsuit", "settlement", "fine", "ruling", "approval",
                     "approves", "recall", "ban", "tariff exemption",
                     "delisting", "non-reliance"]),
    ("earnings",    ["earnings", "quarterly results", "results of operations",
                     "revenue", "guidance", "outlook", "forecast", "beats",
                     "misses", "eps", "profit warning"]),
    ("people",      ["layoff", "layoffs", "job cuts", "cuts jobs", "hires",
                     "hiring", "appoints", "names new", "resigns", "steps down",
                     "fired", "ousted", "new ceo", "new cfo", "chief executive",
                     "departure", "succession", "director change"]),
    ("analyst",     ["upgrade", "downgrade", "price target", "initiates coverage",
                     "overweight", "underweight", "outperform", "underperform",
                     "buy rating", "sell rating", "lifts target", "raises target",
                     "cuts target", "bull case", "bear case"]),
    ("product",     ["launch", "launches", "unveils", "announces new", "releases",
                     "debuts", "introduces", "rollout", "new product", "next-gen",
                     "new model", "new chip", "new service"]),
    ("partnership", ["partnership", "partners with", "deal with", "contract",
                     "agreement", "collaboration", "teams up", "wins order"]),
    ("macro",       ["federal reserve", "fed ", "interest rate", "rate cut",
                     "rate hike", "inflation", "jobs report", "cpi", "gdp",
                     "tariff"]),
]

# Words that escalate impact a notch regardless of category.
_ESCALATION_WORDS = ["halt", "halted", "bankrupt", "fraud", "soars", "plunges",
                     "surges", "crashes", "data breach", "record high",
                     "record low", "all-time"]

# How market-moving a category tends to be, before sentiment is considered.
_CATEGORY_WEIGHT = {"ma": 2, "regulatory": 2, "earnings": 2, "people": 1,
                    "analyst": 1, "product": 1, "partnership": 1, "macro": 1,
                    "general": 0}


def categorize_headline(title: str) -> dict:
    """
    Classify one headline. Returns:
        {"category": str, "sentiment": int, "impact": "high"|"medium"|"low"}

    sentiment is the bullish-minus-bearish keyword count (news_catalyst's
    scorer). impact combines the category's typical market weight, sentiment
    magnitude, and escalation words — it drives the badge color on the News
    tab and whether subscribers get pinged.
    """
    t = (title or "").lower()

    category = "general"
    for cat, words in CATEGORY_KEYWORDS:
        if any(w in t for w in words):
            category = cat
            break

    sentiment = score_headline(title or "")

    impact_score = _CATEGORY_WEIGHT.get(category, 0) + min(2, abs(sentiment))
    if any(w in t for w in _ESCALATION_WORDS):
        impact_score += 1

    impact = "high" if impact_score >= 4 else ("medium" if impact_score >= 2 else "low")
    return {"category": category, "sentiment": sentiment, "impact": impact}


def _item_id(ticker: str, title: str) -> str:
    """Stable dedup key — same headline for the same ticker is one item."""
    raw = f"{ticker.upper()}|{(title or '').strip().lower()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


# ─── Fetch (single seam for sources) ──────────────────────────────────────────

def _fetch_for_ticker(ticker: str, max_articles: int = MAX_HEADLINES_PER_TICKER) -> list[dict]:
    """
    Headlines for one ticker from ALL enabled sources (Tradier, Google News,
    yfinance, SEC), aggregated and deduped by news_sources.fetch_all_news.

    Returns items normalized to
        {"title", "url", "source", "published_ts", "feed", "source_count"}.
    Never raises — a dead/blocked source just contributes nothing.
    """
    try:
        from news_sources import fetch_all_news
        return fetch_all_news(ticker, max_articles=max_articles)
    except Exception as e:
        log.debug("news fetch failed for %s: %s", ticker, e)
        return []


# ─── Focus list (what the monitor watches) ────────────────────────────────────

def _focus_tickers(max_n: int = MAX_FOCUS_TICKERS) -> list[str]:
    """
    Priority order: held positions (news here is risk RIGHT NOW), then the
    tickers currently on the scanner-setups list (we may be about to trade
    them), then the core watchlist. Deduped, capped at max_n.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(t: str | None):
        if t:
            t = str(t).upper()
            if t not in seen:
                seen.add(t)
                ordered.append(t)

    try:
        from risk_manager import _open_underlyings
        for t in _open_underlyings():
            _add(t)
    except Exception:
        pass

    try:
        saved = read_json("scanner_setups.json", {})
        for s in (saved.get("setups", []) if isinstance(saved, dict) else []):
            _add(s.get("ticker"))
    except Exception:
        pass

    for t in CORE_WATCHLIST:
        _add(t)

    return ordered[:max_n]


# ─── Feed maintenance ─────────────────────────────────────────────────────────

def merge_items(existing: list[dict], new_items: list[dict],
                max_items: int = NEWS_MAX_ITEMS) -> tuple[list[dict], list[dict]]:
    """
    Merge fresh items into the stored feed. Dedup by id; newest first; capped.
    Returns (merged_feed, genuinely_new_items). Pure — unit-tested.
    """
    by_id = {item.get("id"): item for item in existing if item.get("id")}
    fresh: list[dict] = []
    for item in new_items:
        iid = item.get("id")
        if iid and iid not in by_id:
            by_id[iid] = item
            fresh.append(item)
    merged = sorted(by_id.values(), key=lambda i: i.get("ts", 0), reverse=True)
    return merged[:max_items], fresh


def sweep_once(tickers: list[str] | None = None) -> list[dict]:
    """
    One full collection pass: fetch → categorize → merge into NEWS_FEED_FILE.
    Returns the genuinely-new items (for alerting). Never raises.
    """
    tickers = tickers or _focus_tickers()
    collected: list[dict] = []
    now_iso = now_est().isoformat(timespec="seconds")

    for ticker in tickers:
        try:
            for art in _fetch_for_ticker(ticker):
                title = art["title"]
                meta = categorize_headline(title)
                ts = art["published_ts"] or int(time.time())
                collected.append({
                    "id":           _item_id(ticker, title),
                    "ticker":       ticker,
                    "title":        title,
                    "url":          art.get("url", ""),
                    "source":       art.get("source", ""),
                    "feed":         art.get("feed", ""),
                    "source_count": art.get("source_count", 1),
                    "ts":           ts,
                    "published":    datetime.datetime.fromtimestamp(
                                        ts, tz=datetime.timezone.utc).isoformat(timespec="seconds"),
                    "category":     meta["category"],
                    "sentiment":    meta["sentiment"],
                    "impact":       meta["impact"],
                    "first_seen":   now_iso,
                })
        except Exception as e:
            log.debug("news sweep failed for %s: %s", ticker, e)

    try:
        with file_lock(NEWS_FEED_FILE):
            data = read_json(NEWS_FEED_FILE, [])
            existing = data if isinstance(data, list) else []
            merged, fresh = merge_items(existing, collected)
            if fresh:
                atomic_write_json(NEWS_FEED_FILE, merged)
    except Exception as e:
        log.warning("could not persist news feed: %s", e)
        return []

    if fresh:
        log.info("news sweep: %d ticker(s), %d new headline(s)", len(tickers), len(fresh))
    return fresh


# ─── High-impact alerts ───────────────────────────────────────────────────────

_ALERTED_IDS: set[str] = set()    # per-process; restart re-arms, acceptable


def alert_high_impact(fresh: list[dict], max_per_msg: int = 5) -> None:
    """Ping subscribers about new HIGH-impact headlines (batched, deduped)."""
    try:
        hot = [i for i in fresh
               if i.get("impact") == "high" and i.get("id") not in _ALERTED_IDS]
        if not hot:
            return
        for i in hot:
            _ALERTED_IDS.add(i["id"])
        hot = hot[:max_per_msg]

        lines = []
        for i in hot:
            arrow = "▲" if i.get("sentiment", 0) > 0 else ("▼" if i.get("sentiment", 0) < 0 else "•")
            lines.append(f"{arrow} {i['ticker']} [{i['category']}]: {i['title']}")
        from event_notifier import _dispatch
        _dispatch(subject=f"High-impact news — {len(hot)} headline(s)",
                  lines=lines, severity="warning")
    except Exception as e:
        log.warning("news alert failed: %s", e)


# ─── Monitor loop ─────────────────────────────────────────────────────────────

def run_monitor() -> None:
    """The polling loop start_all.py runs as the news_monitor component.

    Each cycle: a news sweep (every cycle) + a social-sentiment sweep (every
    SOCIAL_POLL_EVERY cycles — the social APIs rate-limit harder, and chatter
    moves slower than headlines). Both share one focus-ticker list."""
    from heartbeat import beat
    log.info("News monitor starting — polling every %ds, feed -> %s (social every %d cycles)",
             POLL_SECONDS, NEWS_FEED_FILE, SOCIAL_POLL_EVERY)
    sweeps = 0
    while True:
        try:
            beat("news_monitor", status="polling")
            tickers = _focus_tickers()
            fresh = sweep_once(tickers)
            alert_high_impact(fresh)
            if sweeps % SOCIAL_POLL_EVERY == 0:
                try:
                    from social_sentiment import social_sweep_once
                    social_sweep_once(tickers)
                except Exception as e:
                    log.debug("social sweep error: %s", e)
            sweeps += 1
        except KeyboardInterrupt:
            log.info("News monitor stopped by user.")
            break
        except Exception as e:
            log.exception("news monitor error: %s — retrying next cycle", e)
        time.sleep(POLL_SECONDS)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fast categorized news feed.")
    parser.add_argument("--monitor", action="store_true", help="Run the polling loop.")
    parser.add_argument("--once", action="store_true", help="One sweep, then exit.")
    args = parser.parse_args()

    if args.monitor:
        run_monitor()
    elif args.once:
        fresh = sweep_once()
        print(f"Sweep complete — {len(fresh)} new headline(s).")
    else:
        feed = read_json(NEWS_FEED_FILE, [])
        if not isinstance(feed, list) or not feed:
            print("No news collected yet. Run: python news_feed.py --once")
        else:
            for i in feed[:30]:
                print(f"[{i.get('impact', '?'):6s}] {i.get('ticker', ''):6s} "
                      f"({i.get('category', '')}) {i.get('title', '')}")
