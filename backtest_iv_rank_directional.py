"""
backtest_iv_rank_directional.py — validation gate for the DIRECTION-AWARE
IV-rank credit-spread fallback (bull-put in a healthy market, BEAR-CALL when
the market is weak).

Why this exists: the live iv_rank fallback used to sell a bull-put spread on
every sell_premium signal — a bullish position that loses exactly on red days.
select_credit_spread_legs() is now market-aware: when market_regime says the
tape is weak (bear regime OR SPY meaningfully red intraday) it sells a
BEAR-CALL spread above the market instead. This script A/Bs that logic against
the always-bull-put baseline on the same signals.

NOTE on the earlier rejection: backtest_bear_call.py tested bear-call spreads
on the PEAD down-gap drift trigger (the signal family whose long-put version
lost -$12.7k at 8% win). That rejected the SIGNAL, not the structure. Here the
bear-call rides the validated IV-rank >= 75 premium-selling entry — a different
edge that must pass its own gate before ENABLE_BEAR_CALL (iv_rank_bot.py) is
flipped on.

Methodology (mirrors backtest_iv_rank.py, same honest hold-to-expiry model):
  1. yfinance daily data, HV30 as the IV proxy, weekly scan, IVR >= 75 to sell.
  2. Market state per signal day from SPY: bear = close < 200d SMA (same as
     market_regime.is_bear_regime); weak-day = SPY's daily return <= the live
     RED_DAY_WEAK_PCT. Daily bars can't see an intraday recovery, so a day that
     dipped and recovered still counts weak — a conservative approximation
     (more bear-calls tested, not fewer).
  3. Weak day  -> bear-call spread (sell ~5% OTM call / buy ~10% OTM call —
     the live BC_SHORT_OTM_FALLBACK / BC_LONG_OTM placement).
     Healthy day -> bull-put spread (unchanged 5%/10% below).
  4. Black-Scholes leg pricing at entry, intrinsic at expiry, both sides sized
     by budget // max_loss — identical to the validated bull-put model.
  5. Prints the bear-call subset AND the directional-vs-baseline comparison
     against the ACCEPTANCE GATE below.

ACCEPTANCE GATE (repo rule: two independent samples — ARCHITECTURE §1):
  Sample A:  python backtest_iv_rank_directional.py --days 1460
             (4 years incl. the 2022 bear, mega-cap UNIVERSE)
  Sample B:  python backtest_iv_rank_directional.py --days 730 --universe b
             (2 years, disjoint non-tech UNIVERSE_B)
  PASS requires, on BOTH samples:
    • bear-call subset total P&L > 0
    • bear-call subset profit factor >= 1.2
    • bear-call subset win rate >= 60%
    • directional total P&L >= always-bull-put baseline total P&L
  On PASS: flip ENABLE_BEAR_CALL = True in iv_rank_bot.py and record the
  numbers in the constant's comment. On FAIL: leave it False — the defensive
  "skip the bull-put on weak days" behavior stands on its own.
"""

import argparse
import datetime
import json

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from backtest_iv_rank import (
    _bs_call, _compute_hv_rank_series, _score, _bull_put_pnl,
    STARTING_CAPITAL, RISK_PER_TRADE_PCT, SCAN_INTERVAL_DAYS, DTE_TARGET,
    IVR_SELL_THRESHOLD, MIN_SETUP_SCORE, MIN_HV, UNIVERSE,
)

load_dotenv()


# --- Config -------------------------------------------------------------------

# Bear-call strike placement — mirrors the live fallback constants in
# iv_rank_bot.py (BC_SHORT_OTM_FALLBACK / BC_LONG_OTM). Daily data has no
# greeks, so the %-OTM fallback path is what a daily backtest can model.
SELL_CALL_OTM = 1.05
BUY_CALL_OTM  = 1.10

# Live intraday weak threshold (market_regime.RED_DAY_WEAK_PCT), applied to
# SPY's DAILY close-to-close return here (see docstring approximation note).
WEAK_DAY_PCT = -0.75

# Sample-B universe: liquid non-tech mega/large caps, disjoint from UNIVERSE
# (which is tech/growth-heavy) so the two samples are independent.
UNIVERSE_B = [
    "JPM", "BAC", "GS", "WFC", "V", "MA", "AXP",
    "XOM", "CVX", "COP", "SLB",
    "UNH", "JNJ", "PFE", "MRK", "LLY", "ABBV",
    "CAT", "DE", "BA", "GE", "HON", "UPS",
    "WMT", "COST", "HD", "MCD", "NKE", "DIS", "F",
]

# Acceptance-gate thresholds (see module docstring).
GATE_MIN_PF       = 1.2
GATE_MIN_WIN_RATE = 60.0


# --- Bear-call P&L (mirror of backtest_iv_rank._bull_put_pnl) -----------------

def _bear_call_pnl(
    entry: float, exit_px: float, hv: float, dte: int, budget: float
) -> dict:
    """
    Bear-call credit spread held to expiry.
    Sell call at SELL_CALL_OTM*entry, buy call at BUY_CALL_OTM*entry.
    Uses Black-Scholes with hv as IV to price at entry; intrinsic at expiry.
    """
    T = dte / 365.0
    sell_k = entry * SELL_CALL_OTM
    buy_k  = entry * BUY_CALL_OTM

    sell_px = _bs_call(entry, sell_k, T, hv)
    buy_px  = _bs_call(entry, buy_k,  T, hv)
    credit  = sell_px - buy_px

    if credit <= 0.0:
        return {"pnl": 0.0, "contracts": 0, "reason": "no_credit"}

    max_profit_per = credit * 100
    max_loss_per   = (buy_k - sell_k - credit) * 100

    if max_loss_per <= 0.0:
        return {"pnl": 0.0, "contracts": 0, "reason": "invalid_spread"}

    contracts = max(1, int(budget / max_loss_per))

    if exit_px <= sell_k:
        pnl_per = max_profit_per      # expired below the short call: keep it all
        reason  = "full_profit"
    elif exit_px >= buy_k:
        pnl_per = -max_loss_per       # blew through the protection
        reason  = "max_loss"
    else:
        frac    = (buy_k - exit_px) / (buy_k - sell_k)
        pnl_per = -max_loss_per + frac * (max_profit_per + max_loss_per)
        reason  = "partial"

    return {
        "pnl":       round(pnl_per * contracts, 2),
        "contracts": contracts,
        "credit":    round(credit, 4),
        "reason":    reason,
    }


# --- SPY market-state maps -----------------------------------------------------

def _load_spy_state(days: int) -> tuple[dict, dict]:
    """
    Returns (bear_map, weak_map): date -> bool.
      bear_map: SPY close < 200d SMA (same rule as market_regime.is_bear_regime)
      weak_map: SPY daily return <= WEAK_DAY_PCT (daily proxy for the live
                intraday red-day signal)
    Both {} on failure (treated as always-healthy -> pure bull-put baseline).
    """
    start = (datetime.date.today() - datetime.timedelta(days=days + 320)).isoformat()
    try:
        spy = yf.download("SPY", start=start, progress=False, auto_adjust=True)
        if spy.empty:
            return {}, {}
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        close = spy["Close"]
        sma   = close.rolling(200).mean()
        bear  = close < sma                       # NaN SMA compares False
        ret   = close.pct_change() * 100.0
        weak  = ret <= WEAK_DAY_PCT               # NaN compares False
        return ({idx.date(): bool(v) for idx, v in bear.items()},
                {idx.date(): bool(v) for idx, v in weak.items()})
    except Exception as e:
        print(f"  SPY download failed ({e}) — no weak days, pure bull-put run")
        return {}, {}


# --- Metrics -------------------------------------------------------------------

def _subset_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "profit_factor": 0.0, "avg_pnl": 0.0}
    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    return {
        "trades":        len(pnls),
        "wins":          len(wins),
        "win_rate":      round(len(wins) / len(pnls) * 100, 1),
        "total_pnl":     round(sum(pnls), 2),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
        "avg_pnl":       round(sum(pnls) / len(pnls), 2),
    }


# --- Main backtest -------------------------------------------------------------

def run_backtest(
    tickers: list[str] = UNIVERSE,
    lookback_days: int = 730,
    scan_interval: int = SCAN_INTERVAL_DAYS,
    dte: int = DTE_TARGET,
    starting_capital: float = STARTING_CAPITAL,
    show_trades: bool = False,
) -> dict:

    print("\n" + "=" * 72)
    print("  IV-RANK DIRECTIONAL BACKTEST — bull-put vs market-aware fallback")
    print(f"  Universe: {len(tickers)} stocks | Lookback: {lookback_days}d | DTE: {dte}")
    print(f"  Weak day: SPY daily return <= {WEAK_DAY_PCT}% OR bear regime (SPY<200dSMA)")
    print("=" * 72 + "\n")

    bear_map, weak_map = _load_spy_state(lookback_days)
    n_weak = sum(1 for v in weak_map.values() if v)
    n_bear = sum(1 for v in bear_map.values() if v)
    print(f"  SPY state: {n_bear} bear-regime days, {n_weak} weak days in window\n")

    directional: list[dict] = []   # what the new live logic would trade
    baseline:    list[dict] = []   # always-bull-put on the SAME signals

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=lookback_days)
    dl_start = cutoff - datetime.timedelta(days=400)

    for ticker in tickers:
        print(f"[{ticker}] downloading...", end=" ", flush=True)
        try:
            raw = yf.download(
                ticker,
                start=dl_start.strftime("%Y-%m-%d"),
                end=today.strftime("%Y-%m-%d"),
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]
            close = raw["Close"].dropna().astype(float)
        except Exception as e:
            print(f"error: {e}")
            continue

        if len(close) < 300:
            print("skipped (insufficient data)")
            continue

        hv30, iv_rank = _compute_hv_rank_series(close)
        idx_dates = pd.Series(close.index, index=close.index).apply(
            lambda t: t.date() if hasattr(t, "date")
            else datetime.datetime.strptime(str(t)[:10], "%Y-%m-%d").date()
        )

        n_signals = 0
        scan_date = cutoff
        while scan_date <= today - datetime.timedelta(days=dte + 1):
            step = datetime.timedelta(days=scan_interval)
            valid = idx_dates[idx_dates <= scan_date]
            if valid.empty:
                scan_date += step
                continue
            bar_idx = valid.index[-1]

            hv_val  = hv30.get(bar_idx)
            ivr_val = iv_rank.get(bar_idx)
            if (hv_val is None or ivr_val is None
                    or np.isnan(hv_val) or np.isnan(ivr_val)
                    or hv_val < MIN_HV or ivr_val < IVR_SELL_THRESHOLD):
                scan_date += step
                continue
            if _score(ivr_val, hv_val, "sell_premium") < MIN_SETUP_SCORE:
                scan_date += step
                continue

            entry_price = float(close.get(bar_idx, 0))
            if entry_price <= 0:
                scan_date += step
                continue

            exit_target = scan_date + datetime.timedelta(days=dte)
            valid_exit  = idx_dates[idx_dates >= exit_target]
            if valid_exit.empty:
                scan_date += step
                continue
            exit_price = float(close.get(valid_exit.index[0], 0))
            if exit_price <= 0:
                scan_date += step
                continue

            sig_date = idx_dates.get(bar_idx)
            is_weak  = bool(bear_map.get(sig_date, False) or weak_map.get(sig_date, False))
            budget   = starting_capital * RISK_PER_TRADE_PCT   # fixed sizing: pure edge A/B

            bp = _bull_put_pnl(entry_price, exit_price, hv_val, dte, budget)
            bc = _bear_call_pnl(entry_price, exit_price, hv_val, dte, budget)
            dir_result, structure = (bc, "bear_call") if is_weak else (bp, "bull_put")

            if bp["contracts"] > 0:
                baseline.append({"ticker": ticker, "pnl": bp["pnl"]})
            if dir_result["contracts"] > 0:
                move = (exit_price - entry_price) / entry_price * 100
                directional.append({
                    "ticker":     ticker,
                    "date":       sig_date.isoformat(),
                    "structure":  structure,
                    "weak":       is_weak,
                    "iv_rank":    round(float(ivr_val), 1),
                    "hv30_pct":   round(float(hv_val) * 100, 1),
                    "move_pct":   round(move, 2),
                    "pnl":        dir_result["pnl"],
                    "reason":     dir_result["reason"],
                })
                n_signals += 1
                if show_trades:
                    t = directional[-1]
                    print(f"\n  {'WIN ' if t['pnl'] > 0 else 'LOSS'} {ticker} {t['date']}"
                          f" | {structure.upper()} | IVR {t['iv_rank']:.0f}"
                          f" | move {t['move_pct']:+.1f}% | ${t['pnl']:+.0f}", end="")

            scan_date = exit_target + datetime.timedelta(days=1)

        print(f"{n_signals} signals" if n_signals else "no signals")

    # --- Results & acceptance gate ---------------------------------------------

    if not directional:
        print("\n[Backtest] No trades generated.")
        return {}

    bc_trades = [t for t in directional if t["structure"] == "bear_call"]
    bp_trades = [t for t in directional if t["structure"] == "bull_put"]

    s_dir  = _subset_stats(directional)
    s_bc   = _subset_stats(bc_trades)
    s_bp   = _subset_stats(bp_trades)
    s_base = _subset_stats(baseline)

    print("\n" + "=" * 72)
    print("  IV-RANK DIRECTIONAL RESULTS")
    print("=" * 72)
    for name, s in (("Directional (new live logic)", s_dir),
                    ("  └ bear-call subset (weak days)", s_bc),
                    ("  └ bull-put subset (healthy days)", s_bp),
                    ("Baseline (always bull-put)", s_base)):
        pf = s["profit_factor"]
        pf_txt = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(f"  {name:36s} {s['trades']:4d} trades | WR {s['win_rate']:5.1f}% | "
              f"PF {pf_txt:>5s} | avg ${s['avg_pnl']:+8.2f} | total ${s['total_pnl']:+11.2f}")

    checks = [
        ("bear-call total P&L > 0",
         s_bc["trades"] > 0 and s_bc["total_pnl"] > 0),
        (f"bear-call profit factor >= {GATE_MIN_PF}",
         s_bc["trades"] > 0 and s_bc["profit_factor"] >= GATE_MIN_PF),
        (f"bear-call win rate >= {GATE_MIN_WIN_RATE:.0f}%",
         s_bc["trades"] > 0 and s_bc["win_rate"] >= GATE_MIN_WIN_RATE),
        ("directional total >= baseline total",
         s_dir["total_pnl"] >= s_base["total_pnl"]),
    ]
    print("\n  ACCEPTANCE GATE (this sample — must also pass the second sample):")
    for label, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}")
    sample_pass = all(ok for _, ok in checks)
    print(f"\n  SAMPLE {'PASSED' if sample_pass else 'FAILED'} — "
          f"{'run the other sample before flipping ENABLE_BEAR_CALL' if sample_pass else 'ENABLE_BEAR_CALL stays False'}")
    print("=" * 72 + "\n")

    out = {
        "directional": s_dir,
        "bear_call":   s_bc,
        "bull_put":    s_bp,
        "baseline":    s_base,
        "sample_pass": sample_pass,
        "period_days": lookback_days,
        "universe":    tickers,
        "trades":      directional,
    }
    with open("backtest_iv_rank_directional.json", "w") as f:
        json.dump(out, f, indent=2)
    print("[Backtest] Results saved -> backtest_iv_rank_directional.json\n")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IV-rank directional fallback backtest")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Explicit ticker list (overrides --universe)")
    parser.add_argument("--universe", choices=["a", "b"], default="a",
                        help="a = mega-cap tech UNIVERSE (sample A), b = disjoint UNIVERSE_B (sample B)")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--dte",  type=int, default=DTE_TARGET)
    parser.add_argument("--show-trades", action="store_true")
    args = parser.parse_args()

    universe = args.tickers or (UNIVERSE if args.universe == "a" else UNIVERSE_B)
    run_backtest(tickers=universe, lookback_days=args.days, dte=args.dte,
                 show_trades=args.show_trades)
