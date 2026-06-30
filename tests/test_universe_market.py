"""Tests for the full-market universe: universe.market_csv() preference order
and update_universe symbol filtering (warrants/units/preferreds/test issues)."""

import os

import mawitek.data.universe as universe
import mawitek.data.update_universe as uu


# ─── market_csv() preference ────────────────────────────────────────────────────

def test_market_csv_prefers_full_market(tmp_path, monkeypatch):
    here = tmp_path
    (here / "sp500.csv").write_text("Symbol\nAAPL\n")
    (here / "market_universe.csv").write_text("Symbol\nAAPL\nTSLA\n")
    monkeypatch.setattr(universe.os.path, "dirname", lambda _p: str(here))
    assert universe.market_csv() == os.path.join(str(here), "market_universe.csv")


def test_market_csv_falls_back_to_sp500(tmp_path, monkeypatch):
    here = tmp_path
    (here / "sp500.csv").write_text("Symbol\nAAPL\n")   # no market_universe.csv
    monkeypatch.setattr(universe.os.path, "dirname", lambda _p: str(here))
    assert universe.market_csv() == os.path.join(str(here), "sp500.csv")


def test_market_csv_bare_name_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(universe.os.path, "dirname", lambda _p: str(tmp_path))
    # Neither file exists → returns the bare back-compat name.
    assert universe.market_csv() == "sp500.csv"


# ─── scan_csv() preference (liquid → market → sp500) ────────────────────────────

def test_scan_csv_prefers_liquid_universe(tmp_path, monkeypatch):
    (tmp_path / "sp500.csv").write_text("Symbol\nAAPL\n")
    (tmp_path / "market_universe.csv").write_text("Symbol\nAAPL\nTSLA\n")
    (tmp_path / "liquid_universe.csv").write_text("Symbol\nAAPL\n")
    monkeypatch.setattr(universe.os.path, "dirname", lambda _p: str(tmp_path))
    assert universe.scan_csv() == os.path.join(str(tmp_path), "liquid_universe.csv")


def test_scan_csv_falls_back_to_full_market(tmp_path, monkeypatch):
    (tmp_path / "sp500.csv").write_text("Symbol\nAAPL\n")
    (tmp_path / "market_universe.csv").write_text("Symbol\nAAPL\nTSLA\n")  # no liquid file
    monkeypatch.setattr(universe.os.path, "dirname", lambda _p: str(tmp_path))
    assert universe.scan_csv() == os.path.join(str(tmp_path), "market_universe.csv")


# ─── per-scanner independent rotation offsets ───────────────────────────────────

def test_rotation_keys_are_independent(tmp_path, monkeypatch):
    # Each scanner gets its OWN persistent offset so a fast scanner can't consume
    # the rotation and starve a slow one — each sweeps the universe independently.
    monkeypatch.setattr(universe, "_STATE_FILE", str(tmp_path / "rot.json"))
    full = universe.dedupe_symbols(universe.DEFAULT_UNIVERSE)
    a1 = universe.load_universe(limit=10, rotation_key="catalyst")
    b1 = universe.load_universe(limit=10, rotation_key="hft")
    a2 = universe.load_universe(limit=10, rotation_key="catalyst")
    assert a1 == full[:10]
    assert b1 == full[:10]       # hft starts at its OWN offset 0, not after catalyst
    assert a2 == full[10:20]     # catalyst advanced by exactly its own step (independent)


# ─── screener config ────────────────────────────────────────────────────────────

def test_screen_universe_cuts_microcaps_by_dollar_volume():
    import mawitek.data.screen_universe as su
    # The micro-cap / illiquid floor: $5 price, 1M shares, $20M dollar volume.
    assert su.MIN_PRICE == 5.0
    assert su.MIN_AVG_VOLUME == 1_000_000
    assert su.MIN_DOLLAR_VOLUME == 20_000_000


# ─── update_universe._is_common filtering ───────────────────────────────────────

def test_common_stock_kept():
    assert uu._is_common("AAPL", "Apple Inc. Common Stock", "N", include_etfs=True)


def test_etf_kept_when_allowed_dropped_when_not():
    assert uu._is_common("SPY", "SPDR S&P 500 ETF Trust", "Y", include_etfs=True)
    assert not uu._is_common("SPY", "SPDR S&P 500 ETF Trust", "Y", include_etfs=False)


def test_non_common_securities_rejected():
    # Warrants, units, rights, preferreds, notes → not optionable common stock.
    assert not uu._is_common("ABCDW", "Acme Corp - Warrant", "N", include_etfs=True)
    assert not uu._is_common("ABCDU", "Acme Corp - Unit", "N", include_etfs=True)
    assert not uu._is_common("ABCDR", "Acme Corp - Right", "N", include_etfs=True)
    assert not uu._is_common("ABC-A", "Acme Corp 6.5% Preferred Series A", "N", include_etfs=True)


def test_bad_symbol_chars_rejected():
    # Nasdaq suffix markers for non-common share classes.
    assert not uu._is_common("ABC$", "Whatever", "N", include_etfs=True)
    assert not uu._is_common("ABC+", "Whatever", "N", include_etfs=True)
    assert not uu._is_common("", "Empty symbol", "N", include_etfs=True)
