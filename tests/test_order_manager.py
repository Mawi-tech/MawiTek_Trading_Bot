"""Tests for order_manager: order-state interpretation, fills, tagging."""

import mawitek.core.order_manager as om


def test_interpret_filled():
    r = om._interpret({"status": "filled", "exec_quantity": 3, "avg_fill_price": 4.30}, "t", "1")
    assert r is not None and r.ok
    assert r.filled_qty == 3
    assert r.avg_fill_price == 4.30


def test_interpret_rejected():
    r = om._interpret({"status": "rejected", "reason_description": "no buying power"}, "t", "2")
    assert r is not None and not r.ok
    assert "no buying power" in r.reason


def test_interpret_open_is_nonterminal():
    assert om._interpret({"status": "open"}, "t", "3") is None


def test_interpret_partial_still_working():
    # partially_filled with remaining > 0 → keep polling
    r = om._interpret(
        {"status": "partially_filled", "exec_quantity": 1, "remaining_quantity": 2, "avg_fill_price": 4.3},
        "t", "4",
    )
    assert r is None


def test_interpret_partial_complete():
    # partially_filled with remaining 0 → terminal, ok with the filled qty
    r = om._interpret(
        {"status": "partially_filled", "exec_quantity": 1, "remaining_quantity": 0, "avg_fill_price": 4.3},
        "t", "5",
    )
    assert r is not None and r.ok
    assert r.filled_qty == 1
    assert r.partially_filled


def test_interpret_canceled_dead():
    r = om._interpret({"status": "canceled"}, "t", "6")
    assert r is not None and not r.ok


def test_interpret_canceled_after_partial_fill_counts_as_fill():
    # Canceled/expired but contracts DID fill → those are ours, treat as a fill
    r = om._interpret(
        {"status": "canceled", "exec_quantity": 2, "avg_fill_price": 3.10}, "t", "7")
    assert r is not None and r.ok
    assert r.filled_qty == 2
    assert r.avg_fill_price == 3.10


def test_make_order_tag_unique_and_clean():
    t1 = om.make_order_tag("hft_intraday", "NVDA")
    t2 = om.make_order_tag("hft_intraday", "NVDA")
    assert t1 != t2
    # broker-safe: only alphanumerics and dashes
    assert all(c.isalnum() or c == "-" for c in t1)


def test_place_and_confirm_mock_mode(monkeypatch):
    """In MOCK_MODE place_and_confirm returns a simulated fill at the price."""
    monkeypatch.setattr(om, "MOCK_MODE", True)
    r = om.place_and_confirm(
        symbol="AAPL", option_symbol="AAPL260101C00200000", side="buy_to_open",
        quantity=2, order_type="limit", price=4.25, strategy="catalyst_long_call",
        fallback_price=4.25,
    )
    assert r.ok
    assert r.filled_qty == 2
    assert r.avg_fill_price == 4.25


def test_pending_ledger_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(om, "PENDING_FILE", str(tmp_path / "pending.json"))
    om._record_pending("tag-1", {"symbol": "AAPL", "side": "buy_to_open"})
    assert "tag-1" in om._load_pending()
    om._clear_pending("tag-1")
    assert "tag-1" not in om._load_pending()
