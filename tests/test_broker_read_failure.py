"""Broker-read failure handling — a failed read must be distinguishable from a
genuinely-empty result so reconciliation can't nuke the local position book.

Backstory: every tradier_client read swallowed API errors and returned the same
empty/zero a real empty result would. Reconcilers then saw "no broker positions"
and journaled every still-open position as closed_externally, orphaning it from
exit management. strict=True now raises BrokerReadError on a failed read.
"""

import pytest

import mawitek.data.tradier_client as tc
from mawitek.data.tradier_client import BrokerReadError


class _Resp:
    """Minimal stand-in for a requests.Response."""
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


# ── get_open_positions ────────────────────────────────────────────────────────

def test_positions_strict_raises_on_failure(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(tc.requests, "get", boom)

    with pytest.raises(BrokerReadError):
        tc.get_open_positions(strict=True)


def test_positions_nonstrict_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(tc.requests, "get", boom)

    assert tc.get_open_positions() == []          # back-compat: degrade quietly


def test_positions_strict_returns_empty_on_genuine_empty(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    # 200 OK, account really holds nothing → Tradier sends "positions": "null".
    monkeypatch.setattr(tc.requests, "get",
                        lambda *a, **k: _Resp({"positions": "null"}))
    assert tc.get_open_positions(strict=True) == []   # empty is NOT an error


# ── get_account_balance ───────────────────────────────────────────────────────

def test_balance_strict_raises_on_failure(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    def boom(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(tc.requests, "get", boom)

    with pytest.raises(BrokerReadError):
        tc.get_account_balance(strict=True)


def test_balance_strict_raises_on_unusable_equity(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    # 200 OK but a partial/malformed payload with no total_equity — the exact
    # shape that poisoned the equity curve in the 2026-06 incident.
    monkeypatch.setattr(tc.requests, "get",
                        lambda *a, **k: _Resp({"balances": {"total_equity": 0}}))
    with pytest.raises(BrokerReadError):
        tc.get_account_balance(strict=True)


def test_balance_nonstrict_returns_zeros_on_failure(monkeypatch):
    monkeypatch.setattr(tc, "MOCK_MODE", False)
    def boom(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(tc.requests, "get", boom)

    assert tc.get_account_balance() == {"total_equity": 0,
                                        "option_buying_power": 0, "cash": 0}


# ── reconcilers must no-op on a failed read (not nuke the book) ────────────────

def test_pead_reconcile_noops_on_broker_failure(monkeypatch):
    import mawitek.strategies.pead_executor as pe
    monkeypatch.setattr(pe, "MOCK_MODE", False)
    monkeypatch.setattr(pe, "_load_positions",
                        lambda: [{"option_symbol": "AAPL260101C00150000",
                                  "underlying": "AAPL", "entry_price": 1.0,
                                  "quantity": 1, "expiration": "2026-01-01"}])
    def fail(*a, **k):
        raise BrokerReadError("positions read failed")
    monkeypatch.setattr(pe, "get_open_positions", fail)

    journaled, removed = [], []
    monkeypatch.setattr(pe, "record_closed_trade", lambda **k: journaled.append(k))
    monkeypatch.setattr(pe, "_remove_position", lambda s: removed.append(s))

    assert pe.reconcile_pead_positions() == 0      # bailed, did not reconcile
    assert journaled == [] and removed == []        # book untouched


def test_risk_reconcile_noops_on_broker_failure(monkeypatch):
    import mawitek.core.risk_manager as rm
    import mawitek.core.position_manager as pm
    def fail(*a, **k):
        raise BrokerReadError("positions read failed")
    monkeypatch.setattr(rm, "get_open_positions", fail)
    monkeypatch.setattr(pm, "load_positions",
                        lambda: {"AAPL260101C00150000": {"underlying": "AAPL"}})
    removed = []
    monkeypatch.setattr(pm, "remove_position",
                        lambda sym, **k: removed.append(sym))

    assert rm.reconcile_positions_from_broker() == 0
    assert removed == []                            # never closed a live position
