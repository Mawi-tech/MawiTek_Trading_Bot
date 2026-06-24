"""Tests for the news feed: headline categorization, impact, merge/dedup."""

import news_feed as nf


# ── Categorization ───────────────────────────────────────────────────────────

def test_product_launch_categorized():
    r = nf.categorize_headline("Apple unveils next-gen Vision headset at launch event")
    assert r["category"] == "product"


def test_layoffs_categorized_as_people():
    r = nf.categorize_headline("Tech giant announces layoffs affecting 5,000 jobs")
    assert r["category"] == "people"


def test_hiring_categorized_as_people():
    r = nf.categorize_headline("Retailer names new CEO after surprise departure")
    assert r["category"] == "people"


def test_ma_beats_partnership_priority():
    # "deal" alone is partnership, but acquisition language must win.
    r = nf.categorize_headline("Chipmaker agrees to acquisition deal worth $20B")
    assert r["category"] == "ma"


def test_fda_categorized_as_regulatory():
    r = nf.categorize_headline("FDA approval granted for new diabetes drug")
    assert r["category"] == "regulatory"


def test_earnings_beat_is_bullish_sentiment():
    r = nf.categorize_headline("Company beats earnings estimates, raises guidance")
    assert r["category"] == "earnings"
    assert r["sentiment"] > 0


def test_generic_headline_low_impact():
    r = nf.categorize_headline("Shares trade mixed in quiet session")
    assert r["category"] == "general"
    assert r["impact"] == "low"


def test_big_ma_news_high_impact():
    r = nf.categorize_headline("Software maker soars on $50B merger approval win")
    assert r["impact"] == "high"


def test_empty_title_safe():
    r = nf.categorize_headline("")
    assert r["category"] == "general" and r["impact"] == "low"


# ── Merge / dedup ────────────────────────────────────────────────────────────

def _item(iid, ts, **kw):
    return {"id": iid, "ts": ts, "ticker": "AAPL", "title": f"t{iid}", **kw}


def test_merge_dedups_by_id():
    existing = [_item("a", 100)]
    merged, fresh = nf.merge_items(existing, [_item("a", 100), _item("b", 200)])
    assert {i["id"] for i in merged} == {"a", "b"}
    assert [i["id"] for i in fresh] == ["b"]          # only the new one is "fresh"


def test_merge_newest_first():
    merged, _ = nf.merge_items([_item("a", 100)], [_item("b", 300), _item("c", 200)])
    assert [i["id"] for i in merged] == ["b", "c", "a"]


def test_merge_caps_items():
    existing = [_item(f"e{i}", i) for i in range(10)]
    merged, _ = nf.merge_items(existing, [_item("new", 999)], max_items=5)
    assert len(merged) == 5
    assert merged[0]["id"] == "new"                   # newest survives the cap


def test_item_id_stable_and_distinct():
    a = nf._item_id("AAPL", "Apple launches new chip")
    assert a == nf._item_id("aapl", "Apple launches new chip")   # case-insensitive ticker
    assert a != nf._item_id("MSFT", "Apple launches new chip")   # per-ticker distinct
