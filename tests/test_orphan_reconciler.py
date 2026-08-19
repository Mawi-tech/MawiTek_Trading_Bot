"""
Orphan adoption — the missing half of position reconciliation.

Every strategy drops local book rows the broker no longer holds. Nothing went
the other way, so a broker position absent from every book was invisible to all
exit management and stayed that way through expiry. On 2026-08-18 that was eight
real sandbox positions, three days from expiry, one of them in the money and
headed for automatic exercise into a 900-share short.

The safety property these tests exist to protect: adopting must never place an
order by default. Auto-exit is opt-in, so merging this cannot close a position.
"""

import json
import os

import pytest

import orphan_reconciler as orc


# ── OCC parsing ──────────────────────────────────────────────────────────────

def test_parse_occ_round_trips_the_real_symbols():
    got = orc.parse_occ("BLDR260821P00070000")
    assert got == {"underlying": "BLDR", "expiration": "2026-08-21",
                   "option_type": "put", "strike": 70.0}
    call = orc.parse_occ("WDC260821C00510000")
    assert call["option_type"] == "call" and call["strike"] == 510.0


@pytest.mark.parametrize("bad", ["", "AAPL", "NOTASYMBOL", "AAPL260821X00100000",
                                 "AAPL260821C0010000", "AAPL260821C001000000",
                                 "260821C00100000", None])
def test_parse_occ_rejects_junk(bad):
    """Wrong option letter, 7- and 9-digit strikes, and a missing underlying.
    Note AAPL260821C00100000 is NOT junk - it is a valid $100 AAPL call."""
    assert orc.parse_occ(bad) is None


def test_lowercase_and_padded_symbols_are_normalised():
    """Tradier has returned both; the broker payload is not ours to control."""
    assert orc.parse_occ("  bldr260821p00070000  ")["underlying"] == "BLDR"


def test_dte_is_anchored_to_the_eastern_trading_day(monkeypatch):
    import datetime
    monkeypatch.setattr(orc, "today_est", lambda: datetime.date(2026, 8, 18))
    assert orc.dte_for("2026-08-21") == 3
    assert orc.dte_for("2026-08-18") == 0
    assert orc.dte_for("garbage") is None


# ── Ownership map ────────────────────────────────────────────────────────────

def _books(tmp_path):
    """A registry pointing at tmp_path, mirroring the real book shapes."""
    return [
        ("catalyst_long_call", str(tmp_path / "open_positions.json"),    False),
        ("iv_rank",            str(tmp_path / "iv_rank_positions.json"), True),
        ("hft_intraday",       str(tmp_path / "hft_positions.json"),     True),
    ]


def test_held_symbols_reads_dict_and_list_books(tmp_path):
    (tmp_path / "open_positions.json").write_text(json.dumps(
        {"AAPL260821C00200000": {"quantity": 1}}))          # catalyst: keyed BY symbol
    (tmp_path / "hft_positions.json").write_text(json.dumps(
        [{"option_symbol": "TSLA260821P00300000"}]))         # list book
    owned = orc.held_symbols(_books(tmp_path))
    assert owned["AAPL260821C00200000"] == "catalyst_long_call"
    assert owned["TSLA260821P00300000"] == "hft_intraday"


def test_held_symbols_reads_multi_leg_legs(tmp_path):
    """iv_rank spreads nest their legs — a leg is owned, not an orphan."""
    (tmp_path / "iv_rank_positions.json").write_text(json.dumps([{
        "ticker": "WDC",
        "legs": [{"option_symbol": "WDC260821P00420000"},
                 {"option_symbol": "WDC260821P00440000"}],
    }]))
    owned = orc.held_symbols(_books(tmp_path))
    assert owned["WDC260821P00420000"] == "iv_rank"
    assert owned["WDC260821P00440000"] == "iv_rank"


def test_unreadable_book_does_not_turn_its_positions_into_orphans(tmp_path, monkeypatch):
    """The dangerous failure direction. If a book is corrupt we must NOT adopt
    what it holds — that would double-count the position against the risk caps."""
    p = tmp_path / "hft_positions.json"
    p.write_text("{ this is not json")
    monkeypatch.chdir(tmp_path)
    owned = orc.held_symbols(_books(tmp_path))
    assert owned == {}                       # contributes nothing...
    broker = [{"symbol": "TSLA260821P00300000", "quantity": 1, "cost_basis": 500}]
    # ...and the corrupt book's symbol is still reported as an orphan, which is
    # why adoption alerts loudly rather than silently reconciling.
    assert len(orc.find_orphans(broker, _books(tmp_path))) == 1


# ── Detection ────────────────────────────────────────────────────────────────

BROKER_8 = [
    {"symbol": "AMAT260821P00400000", "quantity": 1,  "cost_basis": 2180.00},
    {"symbol": "BLDR260821P00070000", "quantity": 9,  "cost_basis": 2880.00},
    {"symbol": "WDC260821P00440000",  "quantity": -1, "cost_basis": -3710.00},
]


def test_finds_orphans_when_every_book_is_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orphans = orc.find_orphans(BROKER_8, _books(tmp_path))
    assert {o["option_symbol"] for o in orphans} == {
        "AMAT260821P00400000", "BLDR260821P00070000", "WDC260821P00440000"}


def test_owned_positions_are_not_orphans(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hft_positions.json").write_text(json.dumps(
        [{"option_symbol": "AMAT260821P00400000"}]))
    syms = {o["option_symbol"] for o in orc.find_orphans(BROKER_8, _books(tmp_path))}
    assert "AMAT260821P00400000" not in syms


def test_entry_price_is_reconstructed_exactly_from_cost_basis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    by = {o["option_symbol"]: o for o in orc.find_orphans(BROKER_8, _books(tmp_path))}
    assert by["AMAT260821P00400000"]["entry_price"] == 21.80    # 2180 / (1 * 100)
    assert by["BLDR260821P00070000"]["entry_price"] == 3.20     # 2880 / (9 * 100)


def test_short_leg_yields_a_positive_entry_price_and_short_side(tmp_path, monkeypatch):
    """A short leg has negative qty AND negative basis; abs() on both keeps the
    per-contract price positive, like every other book record."""
    monkeypatch.chdir(tmp_path)
    by = {o["option_symbol"]: o for o in orc.find_orphans(BROKER_8, _books(tmp_path))}
    short = by["WDC260821P00440000"]
    assert short["side"] == "short" and short["quantity"] == -1
    assert short["entry_price"] == 37.10                       # abs(-3710) / 100


@pytest.mark.parametrize("row", [
    {"symbol": "AAPL", "quantity": 100, "cost_basis": 20000},   # equity, not an option
    {"symbol": "AMAT260821P00400000", "quantity": 0, "cost_basis": 0},  # closed row
])
def test_non_option_and_zero_quantity_rows_are_skipped(tmp_path, monkeypatch, row):
    monkeypatch.chdir(tmp_path)
    assert orc.find_orphans([row], _books(tmp_path)) == []


# ── Adoption ─────────────────────────────────────────────────────────────────

def test_adoption_writes_the_book_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = orc.adopt_orphans(BROKER_8, _books(tmp_path), notify=False)
    assert len(first) == 3
    assert orc.adopt_orphans(BROKER_8, _books(tmp_path), notify=False) == []
    book = json.loads((tmp_path / orc.ORPHAN_BOOK).read_text())
    assert len(book) == 3, "re-running must not duplicate rows"
    assert {r["strategy"] for r in book} == {"adopted"}


def test_adopted_record_is_honest_about_lost_entry_intent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orc.adopt_orphans(BROKER_8, _books(tmp_path), notify=False)
    rec = json.loads((tmp_path / orc.ORPHAN_BOOK).read_text())[0]
    assert rec["setup_score"] is None
    assert rec["signals"]["origin"] == "orphan_adoption"
    assert "unrecoverable" in rec["signals"]["note"]


def test_adoption_alerts_the_operator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = []
    import event_notifier
    monkeypatch.setattr(event_notifier, "_dispatch", lambda **kw: sent.append(kw))
    orc.adopt_orphans(BROKER_8, _books(tmp_path))
    assert len(sent) == 1
    assert sent[0]["severity"] == "warning"
    body = "\n".join(sent[0]["lines"])
    assert "BLDR260821P00070000" in body
    assert "Automated exits are OFF" in body


def test_a_dead_notifier_cannot_lose_the_adoption(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import event_notifier
    def _boom(**kw):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(event_notifier, "_dispatch", _boom)
    assert len(orc.adopt_orphans(BROKER_8, _books(tmp_path))) == 3
    assert (tmp_path / orc.ORPHAN_BOOK).exists()


# ── The safety property: no orders by default ────────────────────────────────

def test_auto_exit_is_off_by_default():
    """Merging this module must never close a position by surprise."""
    assert orc.ENABLE_ORPHAN_AUTO_EXIT is False


def test_exit_decision_returns_none_while_auto_exit_is_off(monkeypatch):
    monkeypatch.setattr(orc, "ENABLE_ORPHAN_AUTO_EXIT", False)
    # Expired yesterday and still no exit — the flag governs unconditionally.
    assert orc.orphan_exit_decision({"expiration": "2020-01-01"}) is None


def test_dte_floor_fires_only_when_enabled(monkeypatch):
    import datetime
    monkeypatch.setattr(orc, "ENABLE_ORPHAN_AUTO_EXIT", True)
    as_of = datetime.date(2026, 8, 18)
    # DTE 3 > floor of 2 -> hold.
    assert orc.orphan_exit_decision({"expiration": "2026-08-21"}, as_of) is None
    # DTE 2 <= floor -> close, so an ITM option is never auto-exercised.
    assert orc.orphan_exit_decision({"expiration": "2026-08-20"}, as_of) == "orphan_dte_floor"
    assert orc.orphan_exit_decision({"expiration": "2026-08-18"}, as_of) == "orphan_dte_floor"


def test_unparseable_expiry_never_triggers_an_exit(monkeypatch):
    monkeypatch.setattr(orc, "ENABLE_ORPHAN_AUTO_EXIT", True)
    assert orc.orphan_exit_decision({"expiration": ""}) is None
    assert orc.orphan_exit_decision({}) is None


# ── Drift guard ──────────────────────────────────────────────────────────────

def test_book_registry_matches_risk_managers_list():
    """risk_manager keeps its own hardcoded list of books for the correlation cap.
    If a new strategy is added there but not here, its positions would be read as
    orphans and adopted out from under it. Fail loudly instead."""
    import inspect
    import risk_manager
    src = inspect.getsource(risk_manager._open_underlyings)
    ours = {path for _strat, path, _is_list in orc.BOOKS}
    theirs = set(__import__("re").findall(r'_book_underlyings\("([^"]+)"\)', src))
    assert ours == theirs, (
        f"book registry drift.\n  only in orphan_reconciler: {ours - theirs}"
        f"\n  only in risk_manager:      {theirs - ours}")
