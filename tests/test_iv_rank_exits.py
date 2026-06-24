"""Tests for IV-rank exit decisions and multi-leg P&L math."""

import iv_rank_bot as ivr
import trade_journal as tj


def _spread_pos(credit=1.20, qty=2):
    return {
        "id": "p1", "ticker": "AAPL", "strategy": "bull_put_spread",
        "expiration": "2026-07-17", "quantity": qty, "entry_credit": credit,
        "legs": [
            {"symbol": "SHORT", "side": "short", "strike": 185, "type": "put", "entry_price": 1.50},
            {"symbol": "LONG",  "side": "long",  "strike": 180, "type": "put", "entry_price": 0.30},
        ],
    }


def _condor_pos(credit=2.00, qty=3):
    return {
        "id": "p3", "ticker": "SPY", "strategy": "iron_condor",
        "expiration": "2026-07-17", "quantity": qty, "entry_credit": credit,
        "legs": [
            {"symbol": "SP", "side": "short", "strike": 480, "type": "put",  "entry_price": 2.0},
            {"symbol": "LP", "side": "long",  "strike": 475, "type": "put",  "entry_price": 1.0},
            {"symbol": "SC", "side": "short", "strike": 520, "type": "call", "entry_price": 1.8},
            {"symbol": "LC", "side": "long",  "strike": 525, "type": "call", "entry_price": 0.8},
        ],
    }


def _straddle_pos(debit=5.0, qty=1):
    return {
        "id": "p2", "ticker": "NVDA", "strategy": "long_straddle",
        "expiration": "2026-07-17", "quantity": qty, "entry_debit": debit,
        "legs": [
            {"symbol": "CALL", "side": "long", "strike": 800, "type": "call", "entry_price": 2.5},
            {"symbol": "PUT",  "side": "long", "strike": 800, "type": "put",  "entry_price": 2.5},
        ],
    }


# ── Multi-leg P&L ────────────────────────────────────────────────────────────

def test_credit_spread_pnl_winner():
    # Received 1.20 credit, buy back for 0.30 → keep 0.90 × 100 × 2 = +180
    pos = _spread_pos(credit=1.20, qty=2)
    exit_prices = {"SHORT": 0.40, "LONG": 0.10}  # cost to close = 0.30
    pnl_dollar, pnl_pct, entry_ref, exit_ref, sym = ivr._compute_iv_pnl(pos, exit_prices)
    assert pnl_dollar == 180.0
    assert pnl_pct == 75.0
    assert entry_ref == 1.20 and exit_ref == 0.30
    assert sym == "SHORT"


def test_credit_spread_pnl_loser():
    # Credit 1.20, costs 1.50 to close → lose 0.30 × 100 × 2 = -60
    pos = _spread_pos(credit=1.20, qty=2)
    exit_prices = {"SHORT": 2.00, "LONG": 0.50}  # cost to close = 1.50
    pnl_dollar, pnl_pct, *_ = ivr._compute_iv_pnl(pos, exit_prices)
    assert pnl_dollar == -60.0
    assert pnl_pct == -25.0


def test_straddle_pnl_winner():
    # Debit 5.0, closes for 8.0 → +3.0 × 100 = +300
    pos = _straddle_pos(debit=5.0, qty=1)
    exit_prices = {"CALL": 6.0, "PUT": 2.0}  # value 8.0
    pnl_dollar, pnl_pct, *_ = ivr._compute_iv_pnl(pos, exit_prices)
    assert pnl_dollar == 300.0
    assert pnl_pct == 60.0


def test_straddle_pnl_loser():
    pos = _straddle_pos(debit=5.0, qty=1)
    exit_prices = {"CALL": 1.0, "PUT": 1.5}  # value 2.5
    pnl_dollar, pnl_pct, *_ = ivr._compute_iv_pnl(pos, exit_prices)
    assert pnl_dollar == -250.0
    assert pnl_pct == -50.0


# ── Iron condor P&L (4 legs, 2 short / 2 long) ───────────────────────────────

def test_condor_pnl_winner():
    # Credit 2.00; buy back both spreads for net 0.80 → keep 1.20 × 100 × 3 = +360
    pos = _condor_pos(credit=2.00, qty=3)
    # cost to close = (SP - LP) + (SC - LC) = (0.50-0.10) + (0.50-0.10) = 0.80
    exit_prices = {"SP": 0.50, "LP": 0.10, "SC": 0.50, "LC": 0.10}
    pnl_dollar, pnl_pct, entry_ref, exit_ref, sym = ivr._compute_iv_pnl(pos, exit_prices)
    assert pnl_dollar == 360.0
    assert pnl_pct == 60.0
    assert entry_ref == 2.00 and exit_ref == 0.80
    assert sym == "SP"  # first short leg


def test_condor_pnl_loser():
    # One side blows out: cost to close = (3.0-0.2) + (0.1-0.05) = 2.85 > 2.00 credit
    pos = _condor_pos(credit=2.00, qty=1)
    exit_prices = {"SP": 3.00, "LP": 0.20, "SC": 0.10, "LC": 0.05}
    pnl_dollar, *_ = ivr._compute_iv_pnl(pos, exit_prices)
    # (2.00 - 2.85) * 100 * 1 = -85
    assert pnl_dollar == -85.0


def test_condor_uses_spread_exit_decision_tp(monkeypatch):
    # Condor reuses the credit-based exit. cost ≤ 50% of 2.00 credit → take profit
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 0.90)  # ≤ 1.00
    exit_now, reason = ivr._spread_exit_decision(_condor_pos(credit=2.00))
    assert exit_now and "profit" in reason.lower()


def test_condor_stop_loss(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 4.50)  # ≥ 2× credit
    exit_now, reason = ivr._spread_exit_decision(_condor_pos(credit=2.00))
    assert exit_now and "stop" in reason.lower()


def test_cost_to_close_generalizes_to_four_legs(monkeypatch):
    # Σ shorts − Σ longs, priced via _leg_mid
    quotes = {"SP": 0.60, "LP": 0.10, "SC": 0.55, "LC": 0.05}
    monkeypatch.setattr(ivr, "_leg_mid", lambda sym, u, e: quotes[sym])
    cost = ivr._spread_cost_to_close(_condor_pos())
    # (0.60 + 0.55) - (0.10 + 0.05) = 1.00
    assert cost == 1.00


def test_cost_to_close_none_when_short_unpriced(monkeypatch):
    # A short leg with no quote → can't act, return None
    quotes = {"SP": 0.0, "LP": 0.10, "SC": 0.55, "LC": 0.05}
    monkeypatch.setattr(ivr, "_leg_mid", lambda sym, u, e: quotes[sym])
    assert ivr._spread_cost_to_close(_condor_pos()) is None


# ── Iron condor selection (synthetic chain, no network) ──────────────────────

def _contract(opt_type, strike, bid, ask, oi=500):
    return {
        "option_type": opt_type, "strike": strike, "bid": bid, "ask": ask,
        "open_interest": oi, "symbol": f"X{opt_type[0].upper()}{int(strike)}",
    }


def test_select_iron_condor_builds_valid_structure():
    spot = 500.0
    chain = [
        _contract("put", 450, 0.95, 1.05),   # long put  (~0.90*spot)
        _contract("put", 475, 2.95, 3.05),   # short put (~0.95*spot)
        _contract("call", 525, 2.95, 3.05),  # short call(~1.05*spot)
        _contract("call", 550, 0.95, 1.05),  # long call (~1.10*spot)
    ]
    legs = ivr._select_iron_condor(chain, spot, "2026-07-17", 30)
    assert legs is not None
    assert legs["strategy"] == "iron_condor"
    # Strict strike ordering: long put < short put < short call < long call
    assert legs["long_put_strike"] < legs["short_put_strike"] < legs["short_call_strike"] < legs["long_call_strike"]
    # credit = (3.0-1.0) + (3.0-1.0) = 4.0 ; max risk = (25 width - 4 credit)*100
    assert legs["net_credit"] == 4.0
    assert legs["max_risk"] == 2100.0


def test_select_iron_condor_rejects_when_no_call_wing():
    # Only puts available → can't build the call side → None (caller falls back to bull-put)
    spot = 500.0
    chain = [_contract("put", 450, 0.95, 1.05), _contract("put", 475, 2.95, 3.05)]
    assert ivr._select_iron_condor(chain, spot, "2026-07-17", 30) is None


# ── Credit spread exit decisions ─────────────────────────────────────────────

def test_spread_take_profit(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 0.50)  # ≤ 0.60 (50% of 1.20)
    exit_now, reason = ivr._spread_exit_decision(_spread_pos(credit=1.20))
    assert exit_now and "profit" in reason.lower()


def test_spread_stop_loss(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 2.50)  # ≥ 2× credit
    exit_now, reason = ivr._spread_exit_decision(_spread_pos(credit=1.20))
    assert exit_now and "stop" in reason.lower()


def test_spread_hold(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 1.00)  # between TP and SL
    exit_now, _ = ivr._spread_exit_decision(_spread_pos(credit=1.20))
    assert not exit_now


def test_spread_dte_forces_exit(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 5)  # ≤ IVR_MIN_DTE_EXIT
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 1.00)
    exit_now, reason = ivr._spread_exit_decision(_spread_pos())
    assert exit_now and "DTE" in reason


# ── Straddle exit decisions ──────────────────────────────────────────────────

def test_straddle_take_profit(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_straddle_value", lambda p: 7.6)  # ≥ +50% on 5.0
    exit_now, reason = ivr._straddle_exit_decision(_straddle_pos(debit=5.0))
    assert exit_now and "profit" in reason.lower()


def test_straddle_stop_loss(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_straddle_value", lambda p: 2.4)  # ≤ -50% on 5.0
    exit_now, reason = ivr._straddle_exit_decision(_straddle_pos(debit=5.0))
    assert exit_now and "stop" in reason.lower()


def test_straddle_hold(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_straddle_value", lambda p: 5.5)
    exit_now, _ = ivr._straddle_exit_decision(_straddle_pos(debit=5.0))
    assert not exit_now


# ── trade_journal explicit-P&L override ──────────────────────────────────────

def test_record_closed_trade_pnl_override(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", str(tmp_path / "closed.json"))
    # entry/exit prices would compute a DIFFERENT P&L; override must win.
    rec = tj.record_closed_trade(
        option_symbol="SHORT", underlying="AAPL",
        entry_price=1.20, exit_price=0.30, quantity=2,
        expiration="2026-07-17", entry_time=None, exit_reason="take_profit",
        strategy="iv_rank", pnl_dollar=180.0, pnl_pct=75.0,
    )
    assert rec["pnl_dollar"] == 180.0
    assert rec["pnl_pct"] == 75.0


def test_record_closed_trade_default_pnl_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "CLOSED_TRADES_FILE", str(tmp_path / "closed.json"))
    rec = tj.record_closed_trade(
        option_symbol="X", underlying="X",
        entry_price=4.0, exit_price=6.0, quantity=2,
        expiration="2026-07-17", entry_time=None, exit_reason="tp",
    )
    assert rec["pnl_dollar"] == 400.0  # (6-4)*100*2
