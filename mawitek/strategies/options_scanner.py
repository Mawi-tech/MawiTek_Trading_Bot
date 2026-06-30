"""
options_scanner.py

Upgraded scanner for catalyst-driven long call setups.

Filters for:
1. Earnings within 1-5 days
2. Bullish unusual options flow (Unusual Whales)
3. Recent news catalyst
4. Strong price/volume momentum score

Outputs a ranked watchlist of the best setups,
ready for manual or automated call entry.
"""

import datetime
import pandas as pd

from mawitek.data.universe import load_universe
from mawitek.data.market_filter import filter_universe
from mawitek.data.earnings_filter import filter_by_earnings
from mawitek.data.options_flow import get_bullish_sweep_tickers, has_bullish_flow
from mawitek.data.news_catalyst import has_news_catalyst
from mawitek.data.momentum_scorer import score_momentum


# ─── Scanner Config ────────────────────────────────────────────────────────────

EARNINGS_MIN_DAYS = 1
EARNINGS_MAX_DAYS = 5

OPTIONS_FLOW_MIN_PREMIUM = 50_000   # Minimum $ in call sweeps to qualify

NEWS_MIN_SCORE = 1                  # Minimum bullish headline score
NEWS_LOOKBACK_HOURS = 48            # How far back to look for news

MOMENTUM_MIN_SCORE = 40             # Minimum momentum score (0-100)

# Require ALL filters to pass (True = strict mode, False = score-based mode)
STRICT_MODE = False

# In non-strict mode, how many filters must pass to include in output?
MIN_FILTERS_PASSED = 2


# ─── Setup Scoring ─────────────────────────────────────────────────────────────

def score_setup(
    earnings_days: int | None,
    has_flow: bool,
    news_score: int,
    momentum_score: int
) -> int:
    """
    Composite setup score out of 100.

    Weights:
    - Options flow:    35 pts (strongest signal)
    - Earnings timing: 25 pts (closer = better)
    - Momentum:        25 pts (normalized from 0-100 to 0-25)
    - News:            15 pts
    """
    score = 0

    # Options flow
    if has_flow:
        score += 35

    # Earnings timing — closer is better
    if earnings_days is not None:
        if earnings_days == 1:
            score += 25
        elif earnings_days == 2:
            score += 22
        elif earnings_days == 3:
            score += 18
        elif earnings_days <= 5:
            score += 12

    # Momentum (scale 0-100 down to 0-25)
    score += int((momentum_score / 100) * 25)

    # News
    if news_score >= 3:
        score += 15
    elif news_score >= 2:
        score += 10
    elif news_score >= 1:
        score += 5

    return score


# ─── Main Scanner ──────────────────────────────────────────────────────────────

def run_options_scanner(
    csv_path: str | None = "sp500.csv",
    universe_limit: int = 100,
    output_csv: bool = True,
    rotation_key: str | None = None
) -> list[dict]:
    """
    Full pipeline:

    1. Load universe
    2. Filter for liquid stocks
    3. Filter for upcoming earnings
    4. Check options flow, news, momentum
    5. Score and rank setups
    6. Output results

    Returns list of setup dicts sorted by score.
    """
    print("\n" + "="*60)
    print("  OPTIONS CATALYST SCANNER")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

    # Step 1: Universe
    raw_symbols = load_universe(csv_path=csv_path, limit=universe_limit, rotation_key=rotation_key)
    print(f"\n[Scanner] Loaded {len(raw_symbols)} symbols\n")

    # Step 2: Liquidity filter — cut micro-caps / thin names (matches the
    # universe screen so a name that slips through still gets gated live).
    liquid_symbols = filter_universe(
        symbols=raw_symbols,
        min_price=5.0,
        min_avg_volume=1_000_000,
        min_avg_dollar_volume=20_000_000
    )
    print(f"\n[Scanner] {len(liquid_symbols)} symbols passed liquidity filter\n")

    # Step 3: Earnings filter (1-5 days out)
    earnings_setups = filter_by_earnings(
        symbols=liquid_symbols,
        min_days=EARNINGS_MIN_DAYS,
        max_days=EARNINGS_MAX_DAYS
    )

    if not earnings_setups:
        print("[Scanner] No earnings catalysts found in window. Widening to all liquid symbols.")
        # Fall back: scan all liquid symbols (news + flow only)
        earnings_setups = [{"ticker": s, "days_until_earnings": None} for s in liquid_symbols]
    else:
        print(f"\n[Scanner] {len(earnings_setups)} tickers with earnings in {EARNINGS_MIN_DAYS}-{EARNINGS_MAX_DAYS} days\n")

    # Step 4: Pull broad options flow (batch — more efficient than per-ticker)
    print("[Scanner] Fetching unusual options flow...\n")
    flow_tickers = set(
        r["ticker"] for r in get_bullish_sweep_tickers(
            min_premium=OPTIONS_FLOW_MIN_PREMIUM,
            limit=200
        )
    )

    # Step 5: Score each setup
    results = []

    for setup in earnings_setups:
        ticker = setup["ticker"]
        earnings_days = setup["days_until_earnings"]

        print(f"\n[Scanner] Analyzing {ticker}...")

        # Options flow
        flow_confirmed = ticker in flow_tickers
        if not flow_confirmed:
            # Per-ticker check as fallback
            flow_confirmed = has_bullish_flow(ticker, min_premium=OPTIONS_FLOW_MIN_PREMIUM)

        # News catalyst
        news_result = has_news_catalyst(
            ticker,
            min_score=NEWS_MIN_SCORE,
            lookback_hours=NEWS_LOOKBACK_HOURS
        )

        # Momentum
        momentum_result = score_momentum(ticker)
        momentum_score = momentum_result["score"]
        momentum_ok = momentum_score >= MOMENTUM_MIN_SCORE

        # Count filters passed
        filters = {
            "earnings": earnings_days is not None,
            "options_flow": flow_confirmed,
            "news": news_result["has_catalyst"],
            "momentum": momentum_ok
        }
        filters_passed = sum(filters.values())

        # Decide whether to include
        if STRICT_MODE:
            include = all(filters.values())
        else:
            include = filters_passed >= MIN_FILTERS_PASSED

        if not include:
            print(f"[Scanner] {ticker} skipped ({filters_passed}/4 filters passed)")
            continue

        # Composite setup score
        setup_score = score_setup(
            earnings_days=earnings_days,
            has_flow=flow_confirmed,
            news_score=news_result["score"],
            momentum_score=momentum_score
        )

        # ── Day vs swing suitability ──────────────────────────────────────
        # A catalyst setup is inherently a SWING candidate (the catalyst plays
        # out over days). It is ADDITIONALLY flagged day-tradable when intraday
        # momentum + volume are strong enough to scalp while the catalyst
        # builds — that flag is informational for the dashboard/alerts; the
        # actual day-trading entries come from the HFT scanner.
        vol_surge = momentum_result["components"].get("volume_surge") or 0
        day_trade_ok = momentum_score >= 60 and vol_surge >= 1.5

        why_bits = []
        if earnings_days is not None:
            why_bits.append(f"earnings in {earnings_days}d")
        if news_result["has_catalyst"]:
            why_bits.append("news catalyst")
        if flow_confirmed:
            why_bits.append("bullish options flow")
        if momentum_ok:
            why_bits.append(f"momentum {momentum_score}/100")
        style_reason = "swing: " + (", ".join(why_bits) or "catalyst setup")
        if day_trade_ok:
            style_reason += f" · also day-tradable (momentum {momentum_score}, vol {vol_surge:.1f}x)"

        result = {
            "ticker": ticker,
            "setup_score": setup_score,
            "trade_style": "swing",          # catalyst plays are multi-day holds
            "day_trade_ok": day_trade_ok,    # strong enough to also scalp intraday
            "style_reason": style_reason,    # human-readable "why" for dashboard/alerts
            "days_until_earnings": earnings_days,
            "options_flow": flow_confirmed,
            "news_catalyst": news_result["has_catalyst"],
            "news_headline": news_result["top_headline"],
            "momentum_score": momentum_score,
            "filters_passed": filters_passed,
            "filters": filters,              # which individual filters fired (audit detail)
            "rsi": momentum_result["components"].get("rsi", None),
            "roc_5d": momentum_result["components"].get("roc_5d", None),
            "vol_surge": momentum_result["components"].get("volume_surge", None),
        }

        results.append(result)
        print(f"[Scanner] PASS {ticker} | Score: {setup_score}/100 | Filters: {filters_passed}/4")

    # Step 6: Sort by score
    results.sort(key=lambda x: x["setup_score"], reverse=True)

    # Step 7: Output
    print("\n" + "="*60)
    print(f"  TOP SETUPS ({len(results)} found)")
    print("="*60)

    for i, r in enumerate(results, 1):
        print(
            f"\n#{i} {r['ticker']} | Score: {r['setup_score']}/100"
            f"\n    Earnings in: {r['days_until_earnings']}d | "
            f"Flow: {r['options_flow']} | News: {r['news_catalyst']} | "
            f"Momentum: {r['momentum_score']}/100"
            f"\n    RSI: {r['rsi']} | ROC5d: {r['roc_5d']}% | VolSurge: {r['vol_surge']}x"
        )
        if r["news_headline"]:
            print(f"    📰 {r['news_headline']}")

    # Optional CSV export
    if output_csv and results:
        df = pd.DataFrame(results)
        filename = f"setups_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(filename, index=False)
        print(f"\n[Scanner] Results saved to {filename}")

    return results


if __name__ == "__main__":
    run_options_scanner()
