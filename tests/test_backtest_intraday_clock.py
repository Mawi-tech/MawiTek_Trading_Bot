"""Guards for the intraday backtests' option clock (backtest_hft / backtest_vwap_fade).

HV is annualised over trading time (252 sessions x 390 minutes), so the option's
remaining life must be measured on the same clock. These backtests used to convert
session minutes as calendar time (minutes / 1440), which priced a 0-DTE option
~2.3x too cheap — cheap enough that random entries on driftless prices showed a
profit factor above 1. These tests pin the clock so that can't come back.
"""

import math
import random

import numpy as np
import pandas as pd

import backtest_hft as bt
import backtest_vwap_fade as bvf


def test_one_trading_year_is_365_pricing_days():
    assert math.isclose(bt._session_minutes_to_dte_days(390 * 252), 365.0)


def test_atm_0dte_premium_uses_trading_time():
    # ATM (r=0) ≈ S·σ·√T / √(2π), with T in TRADING years.
    S, sigma, mins = 100.0, 0.30, 120
    t_trading = mins / (390 * 252)
    expected = S * sigma * math.sqrt(t_trading) / math.sqrt(2 * math.pi)
    got = bt._bs_price(S, S, sigma, bt._session_minutes_to_dte_days(mins), "call")
    assert math.isclose(got, expected, rel_tol=0.01)
    # The old calendar clock priced the same option ~2.3x cheaper.
    old = bt._bs_price(S, S, sigma, mins / 1440.0, "call")
    assert got / old > 2.2


def test_hist_vol_annualises_by_bar_size():
    # Same per-bar returns: 1-minute bars annualise √5 higher than 5-minute bars.
    rets = np.tile([0.001, -0.001], 20)
    close = pd.Series(100 * np.exp(np.cumsum(rets)))
    v5 = bt._get_hist_iv(close, bar_minutes=5)
    v1 = bt._get_hist_iv(close, bar_minutes=1)
    assert math.isclose(v1 / v5, math.sqrt(5), rel_tol=1e-9)


def test_vwap_fade_backtest_shares_the_clock():
    assert bvf._session_minutes_to_dte_days is bt._session_minutes_to_dte_days


def test_random_entries_do_not_profit():
    # Driftless GBM has no edge, so random 0-DTE entries priced and exited the way
    # backtest_ticker does (same TP/SL/hold/costs) must not show PF > 1.
    rng = np.random.default_rng(7)
    random.seed(1)
    sigma, bars_day, days = 0.30, 78, 150
    dt = 1 / (252 * bars_day)
    px = 100 * np.exp(np.cumsum(rng.normal(-0.5 * sigma**2 * dt, sigma * math.sqrt(dt),
                                           bars_day * days)))
    bar = bt._session_minutes_to_dte_days(5)
    wins = losses = 0.0
    for d in range(1, days):
        for k in range(15, bars_day - bt.MAX_HOLD_BARS, 6):
            i = d * bars_day + k
            kind = random.choice(["call", "put"])
            strike = px[i]
            iv = bt._get_hist_iv(pd.Series(px[i - 30:i + 1]))
            t0 = bt._session_minutes_to_dte_days(390 - 5 * k)
            entry = bt._bs_price(px[i], strike, iv, t0, kind)
            exit_val = None
            for j in range(1, bt.MAX_HOLD_BARS + 1):
                v = bt._bs_price(px[i + j], strike, iv, max(1e-6, t0 - j * bar), kind)
                p = (v - entry) / entry
                if p >= bt.TAKE_PROFIT_PCT or p <= -bt.STOP_LOSS_PCT:
                    exit_val = v
                    break
            if exit_val is None:
                exit_val = bt._bs_price(px[i + bt.MAX_HOLD_BARS], strike, iv,
                                        max(1e-6, t0 - bt.MAX_HOLD_BARS * bar), kind)
            pnl = ((exit_val - entry) * 100 - 2 * bt.COMMISSION_PER_LEG
                   - entry * 100 * bt.SLIPPAGE_PCT)
            if pnl > 0:
                wins += pnl
            else:
                losses -= pnl
    assert wins / losses < 1.0
