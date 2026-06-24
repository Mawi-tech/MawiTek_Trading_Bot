"""Tests for the multi-source news layer: RSS/SEC parsing, dedup, aggregation."""

import news_sources as ns


# ── Normalization ─────────────────────────────────────────────────────────────

def test_norm_title_strips_punct_and_case():
    assert ns._norm_title("NVIDIA Corp. soars 10%!") == "nvidia corp soars 10"


def test_norm_title_collapses_near_dupes():
    a = ns._norm_title("Nvidia soars on earnings beat")
    b = ns._norm_title("Nvidia soars on earnings beat!!!")
    assert a == b and a != ""


# ── Google News RSS ───────────────────────────────────────────────────────────

_GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>Nvidia stock soars on earnings beat - Reuters</title>
      <link>https://news.google.com/rss/articles/abc</link>
      <pubDate>Mon, 16 Jun 2026 12:00:00 GMT</pubDate>
      <source url="https://www.reuters.com">Reuters</source>
    </item>
    <item>
      <title>Apple announces new headset launch - CNBC</title>
      <link>https://news.google.com/rss/articles/def</link>
      <pubDate>Mon, 16 Jun 2026 11:00:00 GMT</pubDate>
      <source url="https://www.cnbc.com">CNBC</source>
    </item>
  </channel>
</rss>"""


def test_parse_google_rss_basic():
    out = ns.parse_google_news_rss(_GOOGLE_RSS)
    assert len(out) == 2
    first = out[0]
    assert first["title"] == "Nvidia stock soars on earnings beat"   # " - Reuters" stripped
    assert first["source"] == "Reuters"
    assert first["feed"] == "google"
    assert first["published_ts"] > 0
    assert first["url"].startswith("https://news.google.com")


def test_parse_google_rss_respects_max():
    out = ns.parse_google_news_rss(_GOOGLE_RSS, max_articles=1)
    assert len(out) == 1


def test_parse_google_rss_bad_xml_safe():
    assert ns.parse_google_news_rss("<not xml") == []


# ── SEC EDGAR 8-K ─────────────────────────────────────────────────────────────

_SEC_DATA = {
    "cik": 320193,
    "filings": {
        "recent": {
            "form":                  ["8-K", "10-Q", "8-K", "4"],
            "filingDate":            ["2026-06-15", "2026-05-01", "2026-04-10", "2026-04-09"],
            "accessionNumber":       ["0000320193-26-000075", "x", "0000320193-26-000060", "y"],
            "items":                 ["2.02,9.01", "", "5.02", ""],
            "primaryDocDescription": ["8-K", "10-Q", "8-K", "FORM 4"],
        }
    },
}


def test_parse_sec_filters_to_8k():
    out = ns.parse_sec_submissions(_SEC_DATA, cik=320193)
    assert len(out) == 2                                  # only the two 8-Ks
    assert all(o["source"] == "SEC EDGAR" and o["feed"] == "sec" for o in out)


def test_parse_sec_maps_item_labels():
    out = ns.parse_sec_submissions(_SEC_DATA, cik=320193)
    assert out[0]["title"] == "SEC 8-K: Results of operations, Financial statements & exhibits"
    assert out[1]["title"] == "SEC 8-K: Executive / director change"


def test_parse_sec_builds_filing_url():
    out = ns.parse_sec_submissions(_SEC_DATA, cik=320193)
    assert out[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000075/0000320193-26-000075-index.htm")


def test_parse_sec_published_ts_set():
    out = ns.parse_sec_submissions(_SEC_DATA, cik=320193)
    assert out[0]["published_ts"] > 0


def test_parse_sec_empty_safe():
    assert ns.parse_sec_submissions({}) == []


def test_sec_items_label_unknown_codes():
    assert ns._sec_items_label("") == ""
    assert ns._sec_items_label("99.99") == ""           # unknown → dropped


# ── Dedup ─────────────────────────────────────────────────────────────────────

def test_dedup_collapses_cross_source_and_counts():
    arts = [
        {"title": "Big merger announced", "url": "u1", "source": "Reuters",
         "published_ts": 200, "feed": "google"},
        {"title": "Big merger announced!!!", "url": "u2", "source": "Yahoo",
         "published_ts": 0, "feed": "yfinance"},                     # near-dup
        {"title": "Apple unveils headset", "url": "u3", "source": "CNBC",
         "published_ts": 50, "feed": "google"},
    ]
    out = ns.dedup_articles(arts)
    assert len(out) == 2
    merger = out[0]
    assert merger["source"] == "Reuters"                # first wins
    assert merger["source_count"] == 2
    assert merger["sources"] == ["Reuters", "Yahoo"]


def test_dedup_backfills_missing_fields():
    arts = [
        {"title": "Same story", "url": "", "source": "A", "published_ts": 0, "feed": "google"},
        {"title": "Same story", "url": "real-url", "source": "B", "published_ts": 999, "feed": "yfinance"},
    ]
    out = ns.dedup_articles(arts)
    assert len(out) == 1
    assert out[0]["url"] == "real-url"          # backfilled from the second copy
    assert out[0]["published_ts"] == 999        # backfilled


def test_dedup_skips_empty_titles():
    out = ns.dedup_articles([{"title": "", "source": "X"}, {"title": "   ", "source": "Y"}])
    assert out == []


# ── Aggregation ───────────────────────────────────────────────────────────────

def test_fetch_all_news_aggregates_all_enabled_sources(monkeypatch):
    monkeypatch.setattr(ns, "fetch_tradier",     lambda t, max_articles=8: [])
    monkeypatch.setattr(ns, "fetch_google_news", lambda t, max_articles=8: [
        {"title": "Big merger announced", "url": "g", "source": "Reuters",
         "published_ts": 200, "feed": "google"}])
    monkeypatch.setattr(ns, "fetch_yfinance",    lambda t, max_articles=8: [
        {"title": "Big merger announced!", "url": "y", "source": "Yahoo",
         "published_ts": 0, "feed": "yfinance"}])
    monkeypatch.setattr(ns, "fetch_sec_edgar",   lambda t, max_articles=8: [
        {"title": "SEC 8-K: Results of operations", "url": "s", "source": "SEC EDGAR",
         "published_ts": 150, "feed": "sec"}])
    for flag in ("ENABLE_TRADIER", "ENABLE_GOOGLE", "ENABLE_YFINANCE", "ENABLE_SEC"):
        monkeypatch.setattr(ns, flag, True)

    out = ns.fetch_all_news("NVDA")
    assert len(out) == 2                                  # merger dup collapsed; SEC distinct
    feeds = {o["feed"] for o in out}
    assert feeds == {"google", "sec"}
    merger = [o for o in out if "merger" in o["title"].lower()][0]
    assert merger["source_count"] == 2


def test_fetch_all_news_disabled_source_skipped(monkeypatch):
    monkeypatch.setattr(ns, "fetch_google_news", lambda t, max_articles=8: [
        {"title": "g story", "source": "G", "feed": "google", "published_ts": 1}])
    monkeypatch.setattr(ns, "fetch_sec_edgar",   lambda t, max_articles=8: [
        {"title": "sec story", "source": "SEC EDGAR", "feed": "sec", "published_ts": 1}])
    monkeypatch.setattr(ns, "ENABLE_TRADIER", False)
    monkeypatch.setattr(ns, "ENABLE_GOOGLE", True)
    monkeypatch.setattr(ns, "ENABLE_YFINANCE", False)
    monkeypatch.setattr(ns, "ENABLE_SEC", False)

    out = ns.fetch_all_news("NVDA")
    assert {o["feed"] for o in out} == {"google"}
