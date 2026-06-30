"""
news_sources.py — pluggable, multi-source news headline layer.

The old news_feed only pulled from Tradier (which 404s in the sandbox) with a
yfinance fallback — effectively ONE working source. This module turns that into
a registry of sources that are AGGREGATED (not first-wins) so the feed gets
genuinely broader coverage, then de-duplicates near-identical stories that the
same event produces across outlets.

Sources (all free, no API keys):
    tradier   — Tradier /markets/news        (production accounts only)
    google    — Google News RSS  per ticker  (aggregates 1000s of outlets)
    yfinance  — Yahoo Finance ticker.news    (works everywhere; minutes lag)
    sec       — SEC EDGAR 8-K filings         (official material-event filings)

Each source returns a list of normalized articles:
    {"title", "url", "source", "published_ts", "feed"}
where `source` is the publisher display name (e.g. "Reuters") and `feed` is the
aggregator it came through (e.g. "google") — the dashboard filters on `feed`.

Toggle a source off with an env var (NEWS_GOOGLE=0, NEWS_SEC=0, …).

The PARSERS (parse_google_news_rss, parse_sec_submissions, dedup_articles) are
pure and unit-tested; the fetch_* functions are thin network wrappers that fail
soft (return [] on any error) so one slow/blocked source never breaks a sweep.
"""

from __future__ import annotations

import datetime
import os
import re
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from mawitek.infra.logger import get_logger
from mawitek.infra.state_io import atomic_write_json, read_json
from mawitek.infra.utils import today_est

log = get_logger("news_sources")

# Contact UA — SEC *requires* a descriptive User-Agent; the others just behave
# better with one than with python-requests' default.
_UA = f"MawiTek Trading Bot/1.0 ({os.getenv('CONTACT_EMAIL', 'contact@example.com')})"
_SESSION = requests.Session()
_TIMEOUT = 10

# ── Source enable flags (env-overridable) ─────────────────────────────────────
ENABLE_TRADIER  = os.getenv("NEWS_TRADIER",  "1") != "0"
ENABLE_GOOGLE   = os.getenv("NEWS_GOOGLE",   "1") != "0"
ENABLE_YFINANCE = os.getenv("NEWS_YFINANCE", "1") != "0"
ENABLE_SEC      = os.getenv("NEWS_SEC",      "1") != "0"


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers (unit-tested)
# ══════════════════════════════════════════════════════════════════════════════

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _norm_title(title: str) -> str:
    """Normalize a headline for cross-source dedup: lowercase, strip punctuation,
    collapse whitespace, truncate. 'NVIDIA Corp. soars 10%!' and 'Nvidia soars
    10 percent' won't match, but 'Nvidia soars on earnings - Reuters' and the
    Yahoo copy of the same wire story will."""
    t = (title or "").lower()
    t = _NON_ALNUM.sub(" ", t)
    return " ".join(t.split())[:80]


def _parse_rfc822(s: str | None) -> int:
    """RSS pubDate ('Mon, 16 Jun 2026 12:00:00 GMT') → Unix ts. 0 on failure."""
    if not s:
        return 0
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        return 0


def _parse_iso_date(s: str | None) -> int:
    """SEC filingDate ('2026-06-16') → Unix ts (UTC midnight). 0 on failure."""
    if not s:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(str(s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def dedup_articles(articles: list[dict]) -> list[dict]:
    """
    Collapse near-duplicate headlines (same story across outlets) by normalized
    title. First occurrence wins (so source order = priority); later copies just
    add their publisher to `sources` and bump `source_count`. Preserves input
    order. Pure.
    """
    seen: dict[str, dict] = {}
    order: list[str] = []

    for a in articles:
        key = _norm_title(a.get("title", ""))
        if not key:
            continue
        if key in seen:
            existing = seen[key]
            src = a.get("source")
            if src and src not in existing["_sources"]:
                existing["_sources"].append(src)
            # Backfill anything the first copy was missing.
            if not existing.get("published_ts") and a.get("published_ts"):
                existing["published_ts"] = a["published_ts"]
            if not existing.get("url") and a.get("url"):
                existing["url"] = a["url"]
        else:
            item = dict(a)
            item["_sources"] = [a["source"]] if a.get("source") else []
            seen[key] = item
            order.append(key)

    out: list[dict] = []
    for key in order:
        item = seen[key]
        srcs = [s for s in item.pop("_sources", []) if s]
        if srcs:
            item["source"] = srcs[0]
            item["source_count"] = len(srcs)
            if len(srcs) > 1:
                item["sources"] = srcs
        else:
            item["source_count"] = 1
        out.append(item)
    return out


# ── Google News RSS ───────────────────────────────────────────────────────────

def parse_google_news_rss(xml_text: str, max_articles: int = 8) -> list[dict]:
    """Parse a Google News RSS document into normalized articles. Pure.

    Google formats <title> as 'Headline - Publisher' and carries the publisher
    again in <source>; we strip the trailing ' - Publisher' so the headline is
    clean and use <source> as the display name."""
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.debug("google rss parse failed: %s", e)
        return out

    for item in root.iter("item"):
        title_el = item.find("title")
        title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
        if not title:
            continue

        link_el = item.find("link")
        url = (link_el.text or "").strip() if link_el is not None and link_el.text else ""

        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""

        # Strip the trailing " - Publisher" Google appends to the headline.
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()
        elif " - " in title:
            head, _, tail = title.rpartition(" - ")
            if head and 0 < len(tail) <= 40:
                title = head.strip()
                if not source:
                    source = tail.strip()

        pub_el = item.find("pubDate")
        ts = _parse_rfc822(pub_el.text if pub_el is not None else None)

        out.append({
            "title": title,
            "url": url,
            "source": source or "Google News",
            "published_ts": ts,
            "feed": "google",
        })
        if len(out) >= max_articles:
            break
    return out


def fetch_google_news(ticker: str, max_articles: int = 8) -> list[dict]:
    """Headlines for a ticker via Google News RSS. Fails soft."""
    try:
        q = quote_plus(f"{ticker} stock")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        r = _SESSION.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        r.raise_for_status()
        return parse_google_news_rss(r.text, max_articles=max_articles)
    except Exception as e:
        log.debug("google news failed for %s: %s", ticker, e)
        return []


# ── SEC EDGAR 8-K filings ─────────────────────────────────────────────────────
# 8-K = "current report" filed for material events. The item code says which
# event; mapping the common ones gives a readable headline.

_SEC_ITEM_LABELS = {
    "1.01": "Entered material agreement",
    "1.02": "Terminated material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completed acquisition or disposition",
    "2.02": "Results of operations",
    "2.03": "Created direct financial obligation",
    "2.04": "Triggering of financial obligation",
    "2.05": "Costs from exit/disposal activities",
    "3.01": "Delisting / listing-rule notice",
    "3.02": "Unregistered equity sale",
    "4.01": "Change in auditor",
    "4.02": "Non-reliance on prior financials",
    "5.01": "Change in control",
    "5.02": "Executive / director change",
    "5.03": "Amended bylaws or fiscal year",
    "5.07": "Shareholder vote results",
    "7.01": "Reg FD disclosure",
    "8.01": "Other material event",
    "9.01": "Financial statements & exhibits",
}


def _sec_items_label(codes: str) -> str:
    """Map an 8-K item-code string ('2.02,9.01') to a human label."""
    if not codes:
        return ""
    labels: list[str] = []
    for c in str(codes).split(","):
        lab = _SEC_ITEM_LABELS.get(c.strip())
        if lab and lab not in labels:
            labels.append(lab)
    return ", ".join(labels)


def _sec_filing_url(cik, accession: str) -> str:
    """Build the filing-index URL for an accession number."""
    try:
        cik_int = int(cik)
    except Exception:
        return "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    acc_nodash = (accession or "").replace("-", "")
    if not acc_nodash:
        return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_int}"
    return (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
            f"{acc_nodash}/{accession}-index.htm")


def parse_sec_submissions(data: dict, cik=None, max_filings: int = 5,
                          forms: tuple[str, ...] = ("8-K",)) -> list[dict]:
    """Parse a SEC submissions JSON blob into normalized filing 'headlines'. Pure.

    The submissions API stores filings as PARALLEL arrays under
    filings.recent (form[i], filingDate[i], items[i], …)."""
    out: list[dict] = []
    try:
        recent = data["filings"]["recent"]
    except Exception:
        return out
    if cik is None:
        cik = data.get("cik")

    forms_arr = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accs = recent.get("accessionNumber", []) or []
    items = recent.get("items", []) or []
    descs = recent.get("primaryDocDescription", []) or []

    for i, form in enumerate(forms_arr):
        if form not in forms:
            continue
        codes = items[i] if i < len(items) else ""
        label = _sec_items_label(codes) or (descs[i] if i < len(descs) else "") or "Current report"
        acc = accs[i] if i < len(accs) else ""
        date = dates[i] if i < len(dates) else ""
        out.append({
            "title": f"SEC {form}: {label}",
            "url": _sec_filing_url(cik, acc),
            "source": "SEC EDGAR",
            "published_ts": _parse_iso_date(date),
            "feed": "sec",
        })
        if len(out) >= max_filings:
            break
    return out


# Ticker → CIK map (cached to disk for the ET day; the map changes rarely).
_CIK_CACHE_FILE = "sec_cik_map.json"
_cik_map: dict[str, int] | None = None


def _load_cik_map() -> dict[str, int]:
    global _cik_map
    if _cik_map is not None:
        return _cik_map

    cached = read_json(_CIK_CACHE_FILE, {})
    today = today_est().isoformat()
    if isinstance(cached, dict) and cached.get("day") == today and isinstance(cached.get("map"), dict):
        _cik_map = cached["map"]
        return _cik_map

    try:
        r = _SESSION.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        raw = r.json()
        m: dict[str, int] = {}
        for v in (raw.values() if isinstance(raw, dict) else []):
            t = str(v.get("ticker", "")).upper()
            if t:
                m[t] = v.get("cik_str")
        _cik_map = m
        atomic_write_json(_CIK_CACHE_FILE, {"day": today, "map": m})
    except Exception as e:
        log.debug("sec cik map fetch failed: %s", e)
        _cik_map = cached.get("map", {}) if isinstance(cached, dict) else {}
    return _cik_map


def fetch_sec_edgar(ticker: str, max_articles: int = 5) -> list[dict]:
    """Recent 8-K filings for a ticker via SEC EDGAR. Fails soft."""
    try:
        cik = _load_cik_map().get(str(ticker).upper())
        if not cik:
            return []
        url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
        r = _SESSION.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        r.raise_for_status()
        return parse_sec_submissions(r.json(), cik=cik, max_filings=max_articles)
    except Exception as e:
        log.debug("sec edgar failed for %s: %s", ticker, e)
        return []


# ── Tradier + yfinance (moved here from news_feed) ────────────────────────────

def fetch_tradier(ticker: str, max_articles: int = 8) -> list[dict]:
    """Tradier /markets/news (production only; sandbox 404s → []). Fails soft."""
    out: list[dict] = []
    try:
        from mawitek.data.market_data import get_news
        for a in get_news(ticker, max_articles=max_articles):
            title = a.get("title", "")
            if title:
                out.append({
                    "title": title,
                    "url": a.get("link", ""),
                    "source": a.get("publisher", "Tradier"),
                    "published_ts": int(a.get("providerPublishTime", 0) or 0),
                    "feed": "tradier",
                })
    except Exception as e:
        log.debug("tradier news failed for %s: %s", ticker, e)
    return out


def fetch_yfinance(ticker: str, max_articles: int = 8) -> list[dict]:
    """Yahoo Finance ticker.news (works everywhere). Fails soft."""
    out: list[dict] = []
    try:
        import yfinance as yf
        from mawitek.data.news_catalyst import _get_article_title, _get_publish_time
        for a in (yf.Ticker(ticker).news or [])[:max_articles]:
            title = _get_article_title(a)
            if not title:
                continue
            ts = _get_publish_time(a) or 0
            content = a.get("content", {}) if isinstance(a.get("content"), dict) else {}
            url = (a.get("link", "")
                   or (content.get("canonicalUrl", {}) or {}).get("url", ""))
            provider = content.get("provider", {}) if isinstance(content.get("provider"), dict) else {}
            source = a.get("publisher", "") or provider.get("displayName", "") or "Yahoo"
            out.append({"title": title, "url": url, "source": source,
                        "published_ts": int(ts), "feed": "yfinance"})
    except Exception as e:
        log.debug("yfinance news failed for %s: %s", ticker, e)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Aggregation
# ══════════════════════════════════════════════════════════════════════════════

def _enabled_sources() -> list[tuple[str, callable]]:
    """Source fetchers in priority order (first-wins on dedup)."""
    srcs: list[tuple[str, callable]] = []
    if ENABLE_TRADIER:
        srcs.append(("tradier", fetch_tradier))
    if ENABLE_GOOGLE:
        srcs.append(("google", fetch_google_news))
    if ENABLE_YFINANCE:
        srcs.append(("yfinance", fetch_yfinance))
    if ENABLE_SEC:
        srcs.append(("sec", fetch_sec_edgar))
    return srcs


def fetch_all_news(ticker: str, max_articles: int = 8) -> list[dict]:
    """
    Pull headlines for a ticker from EVERY enabled source, then dedup near-
    duplicate stories. Each item carries `feed` (aggregator) and `source_count`
    (how many outlets ran it). Never raises.
    """
    collected: list[dict] = []
    for name, fn in _enabled_sources():
        try:
            collected.extend(fn(ticker, max_articles=max_articles))
        except Exception as e:
            log.debug("news source %s failed for %s: %s", name, ticker, e)
    return dedup_articles(collected)
