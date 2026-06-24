"""Tests for social sentiment aggregation (Stocktwits + Reddit) and merge."""

import social_sentiment as ss


# ── Stocktwits ────────────────────────────────────────────────────────────────

def test_aggregate_stocktwits_tags_and_keyword_fallback():
    msgs = [
        {"entities": {"sentiment": {"basic": "Bullish"}}, "body": "x"},
        {"entities": {"sentiment": {"basic": "Bullish"}}, "body": "x"},
        {"entities": {"sentiment": {"basic": "Bearish"}}, "body": "x"},
        {"entities": {"sentiment": {"basic": None}}, "body": "earnings beat upgrade"},  # kw → bull
        {"body": "lawsuit downgrade"},                                                  # kw → bear
        {"body": "just trading sideways"},                                             # neutral
    ]
    r = ss.aggregate_stocktwits(msgs)
    assert r["bullish"] == 3
    assert r["bearish"] == 2
    assert r["neutral"] == 1
    assert r["messages"] == 6
    assert r["score"] == round((3 - 2) / 5, 3)            # tagged-only denominator


def test_aggregate_stocktwits_empty():
    r = ss.aggregate_stocktwits([])
    assert r["messages"] == 0 and r["score"] == 0.0


# ── Reddit ────────────────────────────────────────────────────────────────────

def test_aggregate_reddit_counts_and_clamps():
    posts = [
        {"data": {"title": "AAPL upgrade strong buy record", "selftext": "",
                  "score": 50, "num_comments": 10}},
    ]
    r = ss.aggregate_reddit(posts)
    assert r["mentions"] == 1
    assert r["sentiment_sum"] == 4                        # upgrade+strong+buy+record
    assert r["score"] == 1.0                              # 4/(1*2)=2.0 → clamped to 1.0
    assert r["upvotes"] == 50 and r["comments"] == 10


def test_aggregate_reddit_bare_data_dicts():
    posts = [{"title": "downgrade lawsuit miss", "selftext": ""}]   # no "data" wrapper
    r = ss.aggregate_reddit(posts)
    assert r["mentions"] == 1
    assert r["sentiment_sum"] == -3
    assert r["score"] < 0


def test_aggregate_reddit_empty():
    r = ss.aggregate_reddit([])
    assert r["mentions"] == 0 and r["score"] == 0.0


# ── Combine ───────────────────────────────────────────────────────────────────

def test_combine_social_volume_weighted_bullish():
    st = {"bullish": 8, "bearish": 2, "neutral": 0, "messages": 10, "score": 0.6}
    rd = {"mentions": 2, "sentiment_sum": 0, "score": 0.0, "upvotes": 0, "comments": 0}
    r = ss.combine_social("nvda", st, rd)
    assert r["ticker"] == "NVDA"
    assert r["sentiment_score"] == round((0.6 * 10 + 0.0 * 2) / 12, 3)   # = 0.5
    assert r["net_sentiment"] == "bullish"
    assert r["volume"] == 12


def test_combine_social_bearish_threshold():
    st = {"messages": 5, "score": -0.4}
    r = ss.combine_social("X", st, {})
    assert r["net_sentiment"] == "bearish"


def test_combine_social_neutral_band():
    st = {"messages": 5, "score": 0.1}        # below NEUTRAL_BAND (0.15)
    r = ss.combine_social("X", st, {})
    assert r["net_sentiment"] == "neutral"


def test_combine_social_no_data():
    r = ss.combine_social("X", {}, {})
    assert r["volume"] == 0
    assert r["sentiment_score"] == 0.0
    assert r["net_sentiment"] == "neutral"


# ── Merge ─────────────────────────────────────────────────────────────────────

def test_merge_social_replaces_and_sorts_by_volume():
    existing = [{"ticker": "AAPL", "volume": 5}, {"ticker": "NVDA", "volume": 10}]
    new = [{"ticker": "AAPL", "volume": 20}, {"ticker": "TSLA", "volume": 1}]
    merged = ss.merge_social(existing, new)
    assert [m["ticker"] for m in merged] == ["AAPL", "NVDA", "TSLA"]   # AAPL refreshed to 20
    assert merged[0]["volume"] == 20


def test_merge_social_caps():
    existing = [{"ticker": f"T{i}", "volume": i} for i in range(5)]
    merged = ss.merge_social(existing, [], max_items=3)
    assert [m["ticker"] for m in merged] == ["T4", "T3", "T2"]         # top-3 by volume
