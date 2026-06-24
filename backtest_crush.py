"""
backtest_crush.py  —  validate the EARNINGS IV-CRUSH SELLER (candidate Strategy 6).

The thesis (the inverse of the losing catalyst long-call play): implied vol gets
INFLATED into an earnings print and CRUSHES right after. Instead of BUYING that
expensive premium (negative-EV — see backtest.py), we SELL it with a defined-risk
iron condor the day before earnings (T-1) and buy it back the day after (T+1),
harvesting the crush. We win unless the stock gaps THROUGH a short strike.

Backtest-first, like every other strategy here. This reuses backtest.py's honest
earnings-IV model — entry priced at an INFLATED pre-earnings IV (EARNINGS_IV_INFLATE
× realized HV), exit re-priced at a CRUSHED post-earnings IV (POST_EARNINGS_IV_MULT
× HV) — and, crucially, the REAL historical gap move (actual T+1 close), so a big
surprise produces a real loss capped at the wing width.

    python backtest_crush.py
    python backtest_crush.py --put-otm 0.06 --wing 0.05 --tickers AAPL,MSFT,NVDA

If this validates POSITIVE and robust, it gets wired live (scanner + executor +
own book, reusing iv_rank_bot's condor execution). If not, it joins the rejected
experiments — same discipline as the 4 rejected bearish ideas.
"""

import argparse
import math
import statistics

from backtest import (
    _bs_call_price, _estimate_hist_vol, get_historical_quote,
    get_past_earnings_dates, prev_trading_day, next_trading_day,
    EARNINGS_IV_INFLATE, POST_EARNINGS_IV_MULT, HELD_DAYS, BACKTEST_UNIVERSE,
)

# ── Config (tunable via CLI) ────────────────────────────────────────────────────
DTE_TARGET   = 7       # days to expiry at entry (nearest weekly after the print)
PUT_OTM      = 0.05    # short put ~5% below spot
CALL_OTM     = 0.05    # short call ~5% above spot
WING         = 0.05    # long legs another ~5% further OTM (defines risk)
CONTRACTS    = 1       # condors per trade (P&L scales linearly; sizing is live-side)
LOOKBACK_DAYS = 600    # how far back to pull earnings events


def _bs_put_price(spot: float, strike: float, iv: float, dte_days: int) -> float:
    """Black-Scholes put via put-call parity (r=0): P = C - S + K."""
    call = _bs_call_price(spot, strike, iv, dte_days)
    return round(max(0.01, call - spot + strike), 2)


def _condor_value(spot, sp_k, lp_k, sc_k, lc_k, iv, dte) -> float:
    """
    Net value (per share, ×1) of the SHORT iron condor = what it costs to buy back.
        (short put - long put) + (short call - long call)
    Positive number — the credit you'd pay to close. Entry credit uses the same
    formula at the inflated IV; exit cost uses it at the crushed IV + real spot.
    """
    short_put  = _bs_put_price(spot, sp_k, iv, dte)
    long_put   = _bs_put_price(spot, lp_k, iv, dte)
    short_call = _bs_call_price(spot, sc_k, iv, dte)
    long_call  = _bs_call_price(spot, lc_k, iv, dte)
    return (short_put - long_put) + (short_call - long_call)


def simulate_trade(ticker, earnings_date, put_otm, call_otm, wing,
                   dte_target=DTE_TARGET, em_mult=0.0, em_wing_mult=0.6, max_hv=0.0):
    """
    Sell the condor at T-1, buy it back at T+1. Returns a result dict or None.
    P&L per condor (×100) = entry_credit - exit_cost, floored at -(max wing - credit).

    Strike placement:
      • em_mult <= 0 → FIXED percentage OTM (put_otm / call_otm), wing = `wing`.
      • em_mult  > 0 → EXPECTED-MOVE based: short strikes at em_mult × the name's
        own 1-sigma earnings move (S × inflated-IV × √(dte/365)), wings another
        em_wing_mult × that move beyond. High-vol names get proportionally wider
        strikes, so a NVDA-sized gap is no more likely to breach than an AAPL one.
    """
    entry_date = prev_trading_day(earnings_date, 1)
    exit_date  = next_trading_day(earnings_date, 1)

    s0 = get_historical_quote(ticker, entry_date)
    s1 = get_historical_quote(ticker, exit_date)
    if s0 <= 0 or s1 <= 0:
        return None

    hv = _estimate_hist_vol(ticker, entry_date)
    if hv <= 0:
        return None
    # Skip fat-tail "lottery" names — their earnings gaps run condors over no
    # matter how the strikes are placed. Real premium sellers avoid them.
    if max_hv > 0 and hv > max_hv:
        return None

    iv_entry = hv * EARNINGS_IV_INFLATE
    iv_exit  = hv * POST_EARNINGS_IV_MULT
    dte_exit = max(0.5, dte_target - HELD_DAYS)

    if em_mult > 0:
        # 1-sigma earnings move as a fraction of spot, from the inflated IV.
        em = iv_entry * math.sqrt(dte_target / 365.0)
        put_off  = call_off = em_mult * em
        wing_off = em_wing_mult * em
    else:
        put_off, call_off, wing_off = put_otm, call_otm, wing

    sp_k = round(s0 * (1 - put_off), 2)
    lp_k = round(s0 * (1 - put_off - wing_off), 2)
    sc_k = round(s0 * (1 + call_off), 2)
    lc_k = round(s0 * (1 + call_off + wing_off), 2)
    if not (lp_k < sp_k < sc_k < lc_k):
        return None
    breach_pct = min(put_off, call_off) * 100

    credit = _condor_value(s0, sp_k, lp_k, sc_k, lc_k, iv_entry, dte_target)
    if credit <= 0:
        return None

    exit_cost = _condor_value(s1, sp_k, lp_k, sc_k, lc_k, iv_exit, dte_exit)

    # Defined risk: the widest wing minus the credit collected.
    max_wing  = max(sp_k - lp_k, lc_k - sc_k)
    max_loss  = max_wing - credit
    pnl_share = credit - exit_cost
    pnl_share = max(pnl_share, -max_loss)            # can't lose more than the wing
    pnl_dollar = round(pnl_share * 100 * CONTRACTS, 2)

    move_pct = (s1 - s0) / s0 * 100
    return {
        "ticker": ticker, "earnings_date": earnings_date,
        "credit": round(credit, 2), "exit_cost": round(exit_cost, 2),
        "pnl": pnl_dollar, "move_pct": round(move_pct, 2),
        "max_loss": round(max_loss * 100, 2),
        "breached": abs(move_pct) > breach_pct,   # gapped past a short strike
    }


def run(tickers, put_otm, call_otm, wing, lookback_days, show_trades=False,
        em_mult=0.0, max_hv=0.0, dte_target=DTE_TARGET):
    placement = (f"short {em_mult:.2f}× expected-move / adaptive wings"
                 if em_mult > 0 else f"short ±{put_otm*100:.0f}% / wings +{wing*100:.0f}%")
    print("\n" + "=" * 64)
    print("  EARNINGS IV-CRUSH SELLER BACKTEST — candidate Strategy 6")
    print(f"  Condor: {placement} | {len(tickers)} names | {lookback_days}d lookback")
    print("=" * 64 + "\n")

    trades = []
    for ticker in tickers:
        dates = get_past_earnings_dates(ticker, lookback_days)
        if not dates:
            print(f"  {ticker}: no earnings dates")
            continue
        n = 0
        for ed in dates:
            r = simulate_trade(ticker, ed, put_otm, call_otm, wing,
                               dte_target=dte_target, em_mult=em_mult, max_hv=max_hv)
            if r:
                trades.append(r)
                n += 1
                if show_trades:
                    print(f"    {ticker} {ed}: move {r['move_pct']:+.1f}% | "
                          f"credit ${r['credit']:.2f} -> close ${r['exit_cost']:.2f} | "
                          f"P&L ${r['pnl']:+.0f}" + ("  BREACH" if r['breached'] else ""))
        print(f"  {ticker}: {n} earnings events simulated")

    if not trades:
        print("\nNo trades simulated (no historical data?).")
        return {}

    pnls   = [t["pnl"] for t in trades]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    breaches = [t for t in trades if t["breached"]]

    print("\n" + "=" * 64)
    print("  RESULTS")
    print("=" * 64)
    print(f"  Trades:        {len(trades)}")
    print(f"  Win rate:      {len(wins)/len(trades)*100:.0f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:     ${sum(pnls):+,.0f}  (per 1-condor; avg ${statistics.mean(pnls):+.0f}/trade)")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Avg credit:    ${statistics.mean(t['credit'] for t in trades):.2f}/share")
    print(f"  Strike breaches: {len(breaches)}/{len(trades)} "
          f"({len(breaches)/len(trades)*100:.0f}% gapped past a short strike)")
    if wins:
        print(f"  Avg win:  ${statistics.mean(t['pnl'] for t in wins):+.0f}")
    if losses:
        print(f"  Avg loss: ${statistics.mean(t['pnl'] for t in losses):+.0f}")
    print("=" * 64 + "\n")

    verdict = "POSITIVE" if sum(pnls) > 0 and pf > 1.2 else "WEAK/NEGATIVE"
    print(f"  VERDICT: {verdict} "
          f"(${sum(pnls):+,.0f}, PF {pf:.2f}) — "
          + ("wire it live" if verdict == "POSITIVE" else "do NOT wire; tune or reject") + "\n")
    return {"trades": len(trades), "pnl": sum(pnls), "pf": pf,
            "win_rate": len(wins)/len(trades), "breaches": len(breaches)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Earnings IV-crush seller backtest")
    p.add_argument("--tickers", type=str, default=None, help="Comma-separated (default: BACKTEST_UNIVERSE)")
    p.add_argument("--put-otm", type=float, default=PUT_OTM)
    p.add_argument("--call-otm", type=float, default=CALL_OTM)
    p.add_argument("--wing", type=float, default=WING)
    p.add_argument("--em-mult", type=float, default=0.0,
                   help="Short strikes at this multiple of the expected move (0 = fixed %% mode)")
    p.add_argument("--max-hv", type=float, default=0.0,
                   help="Skip names whose annualized HV exceeds this (0 = no filter)")
    p.add_argument("--dte", type=int, default=DTE_TARGET,
                   help="Days to expiry at entry (lower = closer to expire-after-earnings)")
    p.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    p.add_argument("--show-trades", action="store_true")
    args = p.parse_args()

    universe = args.tickers.split(",") if args.tickers else BACKTEST_UNIVERSE
    run(universe, args.put_otm, args.call_otm, args.wing, args.lookback,
        args.show_trades, em_mult=args.em_mult, max_hv=args.max_hv, dte_target=args.dte)
