"""Tests for the bear-call credit spread and the market-aware vertical
fallback in iv_rank_bot (bull-put in a healthy market, bear-call when weak,
NOTHING when weak and the bear-call can't be built / isn't enabled)."""

import datetime

import iv_rank_bot as ivr


def _call(strike, bid, ask, delta=None, oi=500):
    c = {
        "option_type": "call", "strike": strike, "bid": bid, "ask": ask,
        "open_interest": oi, "symbol": f"XC{int(strike)}",
    }
    if delta is not None:
        c["greeks"] = {"delta": delta}
    return c


def _put(strike, bid, ask, oi=500):
    return {
        "option_type": "put", "strike": strike, "bid": bid, "ask": ask,
        "open_interest": oi, "symbol": f"XP{int(strike)}",
    }


# ── _select_bear_call_spread ─────────────────────────────────────────────────

def test_bear_call_picks_short_leg_by_delta():
    spot = 100.0
    chain = [
        _call(102, 2.95, 3.05, delta=0.45),   # too much delta — outside the band
        _call(105, 2.00, 2.10, delta=0.30),   # the target
        _call(107, 1.40, 1.50, delta=0.22),   # in band but farther from 0.30
        _call(110, 0.56, 0.64, delta=0.10),   # the protection (~1.10*spot)
    ]
    legs = ivr._select_bear_call_spread(chain, spot, "2026-08-21", 30)
    assert legs is not None
    assert legs["strategy"] == "bear_call_spread"
    assert legs["sell_strike"] == 105
    assert legs["buy_strike"] == 110
    # credit = 2.05 - 0.60 = 1.45 ; max risk = (5 - 1.45) * 100
    assert legs["net_credit"] == 1.45
    assert legs["max_risk"] == 355.0


def test_bear_call_falls_back_to_pct_otm_without_greeks():
    spot = 100.0
    chain = [
        _call(103, 2.60, 2.70),
        _call(105, 2.00, 2.10),   # ~5% OTM → the short leg
        _call(110, 0.56, 0.64),   # ~10% OTM → the long leg
    ]
    legs = ivr._select_bear_call_spread(chain, spot, "2026-08-21", 30)
    assert legs is not None
    assert legs["sell_strike"] == 105
    assert legs["buy_strike"] == 110


def test_bear_call_skips_when_credit_too_thin():
    # Credit 0.65 < 20% of the 5-wide spread (1.00) → not paid enough, skip.
    spot = 100.0
    chain = [
        _call(105, 0.75, 0.85, delta=0.30),
        _call(110, 0.12, 0.18, delta=0.10),
    ]
    assert ivr._select_bear_call_spread(chain, spot, "2026-08-21", 30) is None


def test_bear_call_skips_inverted_or_single_strike():
    spot = 100.0
    chain = [_call(110, 0.56, 0.64, delta=0.10)]   # short and long collapse to one strike
    assert ivr._select_bear_call_spread(chain, spot, "2026-08-21", 30) is None


def test_bear_call_respects_liquidity_filters():
    spot = 100.0
    chain = [
        _call(105, 2.00, 2.10, delta=0.30, oi=5),   # OI below MIN_OI_PER_LEG
        _call(110, 0.56, 0.64, delta=0.10, oi=5),
    ]
    assert ivr._select_bear_call_spread(chain, spot, "2026-08-21", 30) is None


# ── Market-aware fallback in select_credit_spread_legs ───────────────────────

def _wire_chain(monkeypatch, chain):
    exp = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    monkeypatch.setattr(ivr, "get_options_expirations", lambda t: [exp])
    monkeypatch.setattr(ivr, "get_options_chain", lambda t, e: chain)
    monkeypatch.setattr(ivr, "PREFER_IRON_CONDOR", False)


def _vertical_chain():
    return [
        _put(95, 2.00, 2.10),
        _put(90, 0.95, 1.05),
        _call(105, 2.00, 2.10, delta=0.30),
        _call(110, 0.56, 0.64, delta=0.10),
    ]


def test_fallback_is_bull_put_in_healthy_market(monkeypatch):
    _wire_chain(monkeypatch, _vertical_chain())
    monkeypatch.setattr(ivr, "_market_is_weak", lambda: False)
    legs = ivr.select_credit_spread_legs("AAPL", 100.0, "sell_premium")
    assert legs is not None and legs["strategy"] == "bull_put_spread"


def test_fallback_is_bear_call_in_weak_market_when_enabled(monkeypatch):
    _wire_chain(monkeypatch, _vertical_chain())
    monkeypatch.setattr(ivr, "_market_is_weak", lambda: True)
    monkeypatch.setattr(ivr, "ENABLE_BEAR_CALL", True)
    legs = ivr.select_credit_spread_legs("AAPL", 100.0, "sell_premium")
    assert legs is not None and legs["strategy"] == "bear_call_spread"


def test_weak_market_never_falls_back_to_bull_put(monkeypatch):
    # Bear-call disabled (pre-validation default): a weak market must yield
    # NOTHING — placing the bullish bull-put is exactly the red-day bleed.
    _wire_chain(monkeypatch, _vertical_chain())
    monkeypatch.setattr(ivr, "_market_is_weak", lambda: True)
    monkeypatch.setattr(ivr, "ENABLE_BEAR_CALL", False)
    assert ivr.select_credit_spread_legs("AAPL", 100.0, "sell_premium") is None


def test_weak_market_unbuildable_bear_call_yields_nothing(monkeypatch):
    # Calls too illiquid to build the bear-call → None, not a bull-put.
    chain = [
        _put(95, 2.00, 2.10),
        _put(90, 0.95, 1.05),
        _call(105, 2.00, 2.10, delta=0.30, oi=5),
        _call(110, 0.56, 0.64, delta=0.10, oi=5),
    ]
    _wire_chain(monkeypatch, chain)
    monkeypatch.setattr(ivr, "_market_is_weak", lambda: True)
    monkeypatch.setattr(ivr, "ENABLE_BEAR_CALL", True)
    assert ivr.select_credit_spread_legs("AAPL", 100.0, "sell_premium") is None


def test_market_weak_check_fails_open_to_bull_path(monkeypatch):
    def boom():
        raise RuntimeError("regime unavailable")
    import market_regime as mr
    monkeypatch.setattr(mr, "is_market_weak", boom)
    assert ivr._market_is_weak() is False


# ── Exits & P&L reuse the credit-spread plumbing ─────────────────────────────

def _bear_call_pos(credit=1.45, qty=2):
    return {
        "id": "bc1", "ticker": "AAPL", "strategy": "bear_call_spread",
        "expiration": "2026-08-21", "quantity": qty, "entry_credit": credit,
        "legs": [
            {"symbol": "SHORTC", "side": "short", "strike": 105, "type": "call", "entry_price": 2.05},
            {"symbol": "LONGC",  "side": "long",  "strike": 110, "type": "call", "entry_price": 0.60},
        ],
    }


def test_bear_call_pnl_winner():
    # Credit 1.45, buy back for 0.40 → keep 1.05 × 100 × 2 = +210
    pnl_dollar, pnl_pct, entry_ref, exit_ref, sym = ivr._compute_iv_pnl(
        _bear_call_pos(), {"SHORTC": 0.50, "LONGC": 0.10})
    assert pnl_dollar == 210.0
    assert entry_ref == 1.45 and exit_ref == 0.40
    assert sym == "SHORTC"


def test_bear_call_pnl_loser():
    # Stock ripped: costs 3.00 to close → lose 1.55 × 100 × 2 = -310
    pnl_dollar, *_ = ivr._compute_iv_pnl(
        _bear_call_pos(), {"SHORTC": 3.40, "LONGC": 0.40})
    assert pnl_dollar == -310.0


def test_bear_call_take_profit_decision(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 0.50)  # ≤ 50% of credit
    exit_now, reason = ivr._spread_exit_decision(_bear_call_pos(credit=1.45))
    assert exit_now and "profit" in reason.lower()


def test_bear_call_stop_loss_decision(monkeypatch):
    monkeypatch.setattr(ivr, "_dte_for", lambda e: 30)
    monkeypatch.setattr(ivr, "_spread_cost_to_close", lambda p: 3.10)  # ≥ 2× credit
    exit_now, reason = ivr._spread_exit_decision(_bear_call_pos(credit=1.45))
    assert exit_now and "stop" in reason.lower()


def test_monitor_recognizes_bear_call(monkeypatch):
    # The monitor's structure tuple must include bear_call_spread, or an open
    # position would sit unmanaged forever.
    closed = []
    monkeypatch.setattr(ivr, "_load_iv_positions", lambda: [_bear_call_pos()])
    monkeypatch.setattr(ivr, "_spread_exit_decision", lambda p: (True, "tp"))
    monkeypatch.setattr(ivr, "_close_iv_position", lambda p, r: closed.append(p["id"]))
    ivr.monitor_iv_rank_positions()
    assert closed == ["bc1"]
