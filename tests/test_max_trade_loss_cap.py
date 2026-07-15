"""Tests for the per-trade worst-case loss cap.

MAX_TRADE_LOSS_PCT decouples a SINGLE trade's max loss from the daily-loss halt,
so one defined-risk spread gapping to its max loss can no longer, by itself, trip
the day's circuit breaker (the COHR bull-put failure mode).
"""

import risk_manager as rm


def test_max_trade_loss_dollars_is_pct_of_equity():
    # 1.5% of equity by default (no per-tier override present).
    assert rm.max_trade_loss_dollars(100_000) == 100_000 * rm.MAX_TRADE_LOSS_PCT
    assert rm.max_trade_loss_dollars(100_000) == 1_500.0


def test_cap_clamps_quantity_to_loss_ceiling():
    # $720 max risk/contract against a $1,500 cap → at most 2 (2 * 720 = 1,440).
    assert rm.cap_contracts_by_max_loss(4, 720.0, 100_000) == 2


def test_cap_leaves_quantity_untouched_when_within_ceiling():
    # 1 contract of $500 worst-case loss is well under the $1,500 cap.
    assert rm.cap_contracts_by_max_loss(1, 500.0, 100_000) == 1


def test_cap_returns_zero_when_one_contract_breaches_cap():
    # A single contract risking $2,000 exceeds the $1,500 cap → skip (0), so the
    # caller declines the trade rather than knowingly exceed the per-trade limit.
    assert rm.cap_contracts_by_max_loss(3, 2_000.0, 100_000) == 0


def test_cap_fails_open_on_unknown_per_contract_risk():
    # Undefined/zero per-contract risk → nothing to cap against, qty untouched.
    assert rm.cap_contracts_by_max_loss(5, 0.0, 100_000) == 5


def test_cohr_scenario_is_capped_from_four_to_one():
    # The trade that halted the day: 10-wide spread, 2.80 credit → $720 max risk
    # per contract. On the ~$78k account the 1.5% cap ($1,170) permits only ONE
    # contract, not the four that were sized — worst case ~$720 instead of ~$2,880,
    # comfortably below the 5% daily halt.
    assert rm.cap_contracts_by_max_loss(4, 720.0, 78_000) == 1
