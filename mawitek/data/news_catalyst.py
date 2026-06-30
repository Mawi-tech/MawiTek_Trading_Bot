"""
news_catalyst.py

Detects recent news catalysts for a ticker using yfinance.
Scores headlines for bullish sentiment using keyword matching.

For a more powerful version, swap the scorer with an LLM call
or integrate a paid news API (Benzinga, Unusual Whales news feed).
"""

import datetime

from mawitek.data.market_data import get_news as _tradier_news


# Keywords that suggest a bullish catalyst
BULLISH_KEYWORDS = [
    "beat", "beats", "record", "raised", "upgrade", "upgraded",
    "outperform", "buy", "bullish", "breakthrough", "partnership",
    "deal", "contract", "approval", "approved", "fda", "wins",
    "guidance raised", "strong", "better than expected", "surge",
    "rally", "positive", "exceed", "exceeds", "milestone"
]

# Keywords that suggest a bearish or neutral catalyst (used to downweight)
BEARISH_KEYWORDS = [
    "miss", "misses", "downgrade", "downgraded", "cut", "loss",
    "decline", "drop", "concern", "warning", "layoff", "lawsuit",
    "investigation", "recall", "disappointing", "below expected"
]


def _get_article_title(article: dict) -> str:
    """Extract headline, checking both top-level and nested content keys."""
    title = article.get("title", "")
    if not title:
        content = article.get("content", {})
        if isinstance(content, dict):
            title = content.get("title", "")
    return title or ""


def _get_publish_time(article: dict) -> int | None:
    """
    Extract Unix publish timestamp, tolerating changes across yfinance versions.

    yfinance <=0.2.42 : top-level 'providerPublishTime' (int)
    yfinance  0.2.43+ : may nest under 'content' with ISO string keys
    """
    for key in ("providerPublishTime", "published", "timestamp", "time"):
        val = article.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)

    content = article.get("content", {})
    if isinstance(content, dict):
        for key in ("pubDate", "publishedAt", "displayTime", "date"):
            val = content.get(key)
            if val and isinstance(val, str):
                try:
                    dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return int(dt.timestamp())
                except Exception:
                    pass

    return None


def fetch_recent_news(ticker: str, max_articles: int = 10) -> list[dict]:
    """
    Fetch recent news headlines via Tradier /markets/news.

    Returns a list of dicts with keys: title, providerPublishTime, link, publisher
    (same shape as the old yfinance response so the rest of this module is unchanged)
    """
    return _tradier_news(ticker, max_articles=max_articles)


def score_headline(title: str) -> int:
    """
    Score a headline based on keyword matching.

    +1 for each bullish keyword found
    -1 for each bearish keyword found
    Returns net score.
    """
    title_lower = title.lower()
    score = 0

    for kw in BULLISH_KEYWORDS:
        if kw in title_lower:
            score += 1

    for kw in BEARISH_KEYWORDS:
        if kw in title_lower:
            score -= 1

    return score


def has_news_catalyst(
    ticker: str,
    min_score: int = 1,
    lookback_hours: int = 48,
    max_articles: int = 10
) -> dict:
    """
    Check if a ticker has a recent bullish news catalyst.

    Args:
        ticker: Stock symbol
        min_score: Minimum bullish score to qualify (default 1)
        lookback_hours: Only consider news within this window (default 48h)
        max_articles: Max headlines to pull

    Returns:
        {
            "has_catalyst": bool,
            "score": int,
            "top_headline": str or None,
            "article_count": int
        }
    """
    articles = fetch_recent_news(ticker, max_articles=max_articles)

    if not articles:
        return {
            "has_catalyst": False,
            "score": 0,
            "top_headline": None,
            "article_count": 0
        }

    # utcnow() was deprecated in Python 3.12; use an explicit tz-aware UTC now
    # so the Unix timestamp comparison is correct on every Python version.
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cutoff = now - (lookback_hours * 3600)

    # Detect whether any article has a parseable timestamp so we can decide
    # whether to apply the time filter at all.
    has_timestamps = any(_get_publish_time(a) is not None for a in articles)
    if not has_timestamps:
        print(f"[News] {ticker} — no timestamps found in articles (yfinance API change?); "
              f"scoring all {len(articles)} articles without time filter")

    total_score = 0
    recent_count = 0
    top_headline = None
    top_score = 0

    for article in articles:
        pub_time = _get_publish_time(article)

        # Apply time filter only when timestamps are available
        if has_timestamps:
            if pub_time is None or pub_time < cutoff:
                continue

        title = _get_article_title(article)
        if not title:
            continue

        score = score_headline(title)
        total_score += score
        recent_count += 1

        if score > top_score:
            top_score = score
            top_headline = title

    has_catalyst = total_score >= min_score

    print(
        f"[News] {ticker} | "
        f"Recent articles: {recent_count} | "
        f"Score: {total_score} | "
        f"Catalyst: {has_catalyst}"
    )

    if top_headline:
        print(f"[News] {ticker} Top headline: {top_headline}")

    return {
        "has_catalyst": has_catalyst,
        "score": total_score,
        "top_headline": top_headline,
        "article_count": recent_count
    }
