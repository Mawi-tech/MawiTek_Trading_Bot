"""Tests for the separate swing/day position budgets."""

import json
import risk_manager as rm


# ── trade type mapping ───────────────────────────────────────────────────────

def test_catalyst_is_swing():
    assert rm.trade_type_for("catalyst_long_call") == "swing"


def test_iv_rank_is_swing():
    assert rm.trade_type_for("iv_rank") == "swing"


def test_hft_is_day():
    assert rm.trade_type_for("hft_intraday") == "day"


def test_unknown_defaults_to_swing():
    assert rm.trade_type_for(None) == "swing"
    assert rm.trade_type_for("something_else") == "swing"


def test_caps_sum_to_total():
    # Swing book holds 4 strategies (catalyst + iv_rank + pead + bounce); day = hft.
    assert rm.MAX_SWING_POSITIONS == 8
    assert rm.MAX_DAY_POSITIONS == 5
    assert rm.MAX_OPEN_POSITIONS == rm.MAX_SWING_POSITIONS + rm.MAX_DAY_POSITIONS


# ── counting by type from local books ────────────────────────────────────────

def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def test_day_count_reads_hft_book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("hft_positions.json", [{"option_symbol": "A"}, {"option_symbol": "B"}])
    assert rm.count_positions_by_type("day") == 2


def test_swing_count_sums_catalyst_and_iv_rank(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("open_positions.json", {"SYM1": {}, "SYM2": {}})          # 2 catalyst
    _write("iv_rank_positions.json", [{"id": "p1"}])                  # 1 iv-rank
    assert rm.count_positions_by_type("swing") == 3                   # 2 + 1


def test_multileg_iv_rank_counts_as_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # An iron condor (4 legs) is ONE entry in the book → one position
    _write("iv_rank_positions.json", [{"id": "p1", "legs": [1, 2, 3, 4]}])
    assert rm.count_positions_by_type("swing") == 1


def test_counts_zero_when_no_books(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rm.count_positions_by_type("swing") == 0
    assert rm.count_positions_by_type("day") == 0


def test_swing_and_day_are_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A full swing book must NOT affect the day count
    _write("open_positions.json", {f"S{i}": {} for i in range(5)})   # swing full (5)
    _write("hft_positions.json", [])                                  # day empty
    assert rm.count_positions_by_type("swing") == 5
    assert rm.count_positions_by_type("day") == 0   # day still has room
