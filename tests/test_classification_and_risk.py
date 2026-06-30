"""Tests for position classification (spread detection) and risk sizing math."""

import mawitek.dashboard.dashboard_state as ds
import mawitek.core.risk_manager as rm


def _leg(qty, opt_type, strike):
    return {
        "quantity": qty,
        "opt_type": opt_type,
        "opt_type_label": "Call" if opt_type == "C" else "Put",
        "strike": float(strike),
    }


# ── Position classification ──────────────────────────────────────────────────

def test_single_long_call():
    assert ds._classify_position_type([_leg(1, "C", 200)]) == "Long Call"


def test_single_long_put():
    assert ds._classify_position_type([_leg(1, "P", 150)]) == "Long Put"


def test_single_short_put():
    assert ds._classify_position_type([_leg(-2, "P", 150)]) == "Short Put"


def test_bull_put_spread():
    # long lower strike put + short higher strike put = bull put (credit)
    legs = [_leg(15, "P", 185), _leg(-15, "P", 195)]
    assert ds._classify_position_type(legs) == "Bull Put Spread"


def test_bear_call_spread():
    # short lower strike call + long higher strike call = bear call (credit)
    legs = [_leg(-1, "C", 100), _leg(1, "C", 110)]
    assert ds._classify_position_type(legs) == "Bear Call Spread"


def test_bull_call_spread():
    legs = [_leg(1, "C", 100), _leg(-1, "C", 110)]
    assert ds._classify_position_type(legs) == "Bull Call Spread"


def test_bear_put_spread():
    legs = [_leg(1, "P", 110), _leg(-1, "P", 100)]
    assert ds._classify_position_type(legs) == "Bear Put Spread"


def test_ratio_spread():
    legs = [_leg(3, "P", 220), _leg(-2, "P", 235), _leg(-1, "P", 240)]
    assert ds._classify_position_type(legs) == "Ratio / Complex Spread"


def test_iron_condor():
    legs = [_leg(-1, "P", 90), _leg(1, "P", 85), _leg(-1, "C", 110), _leg(1, "C", 115)]
    assert "Iron" in ds._classify_position_type(legs)


def test_occ_parse_roundtrip():
    parsed = ds._parse_occ_symbol("QCOM260618P00220000")
    assert parsed["underlying"] == "QCOM"
    assert parsed["type"] == "P"
    assert parsed["strike"] == 220.0
    assert parsed["expiry"] == "2026-06-18"


def test_occ_parse_rejects_garbage():
    assert ds._parse_occ_symbol("NOTANOPTION") is None


# ── Risk sizing math ─────────────────────────────────────────────────────────

def test_position_size_caps_at_max_pct():
    # risk 2% vs max 5% — should take the smaller (risk) amount
    size = rm.get_position_size(100_000)
    assert size == 100_000 * rm.RISK_PER_TRADE_PCT


def test_calculate_contracts_rounds_down():
    # budget 1000, contract costs 4.25*100 = 425 → floor(1000/425) = 2
    assert rm.calculate_contracts(1000, 4.25) == 2


def test_calculate_contracts_zero_when_too_expensive():
    assert rm.calculate_contracts(100, 4.25) == 0


def test_calculate_contracts_zero_price_safe():
    assert rm.calculate_contracts(1000, 0) == 0
