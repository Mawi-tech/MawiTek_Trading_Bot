"""
bounce_scanner.py  —  Strategy 5: Capitulation-Bounce signal.

The bot's bear-market offense. It reuses the EXACT oversold down-gap that the
failed PEAD short used (pead_scanner.detect_drift's "bearish" setup), but:

  1. only fires in a BEAR regime (SPY < 200-day SMA), and
  2. frames it as a LONG opportunity — in a bear market that gap is capitulation
     that snaps back, so we BUY the bounce instead of shorting a drift that
     doesn't happen.

Hard-gated to bear regimes because the identical setup LOSES in a bull market
(there a down-gap is genuine bad news that keeps falling). Validated in
backtest_bounce.py: ~54% win, PF ~3 in bear regimes over 4 years.
"""

from logger import get_logger
from market_regime import is_bear_market
import pead_scanner as ps

log = get_logger("bounce_scanner")

# Reuse PEAD's quality floor — same detector, same threshold.
MIN_SETUP_SCORE = ps.MIN_SETUP_SCORE


def scan_ticker(ticker: str) -> dict | None:
    """
    A bear-regime oversold down-gap, returned as a LONG bounce setup, or None.

    Reuses pead_scanner.scan_ticker (with the daily-bar cache and detect_drift),
    passing bearish_allowed=True so the oversold down-gap is surfaced — then
    re-frames it as a long bounce. The caller only invokes this in a bear regime.
    """
    setup = ps.scan_ticker(ticker, bearish_allowed=True)
    if not setup or setup.get("direction") != "bearish":
        return None
    # Re-frame the bearish down-gap as a long bounce (we buy calls on the rebound).
    setup["signal"] = "capitulation_bounce"
    setup["trade_direction"] = "bullish"
    setup["trade_style"] = "swing"      # days-long bounce hold
    setup["style_reason"] = (f"swing: bear-regime capitulation bounce — oversold "
                             f"{setup['event_move']:+.1f}% gap {setup['days_since']}d ago, "
                             f"buying the snapback ({setup['conviction']} conviction)")
    return setup


def run_bounce_scan(csv_path: str | None = "sp500.csv",
                    universe_limit: int = 120,
                    min_score: int = MIN_SETUP_SCORE,
                    rotation_key: str | None = None) -> list[dict]:
    """
    Scan for bounce setups — but ONLY in a bear regime (returns [] otherwise, so
    the strategy is dormant in bull markets where this trade loses).
    """
    if not is_bear_market():
        log.info("Bull/neutral regime — bounce scanner idle (this trade only wins in bear markets).")
        return []

    from universe import load_universe
    from market_filter import filter_universe

    symbols = load_universe(csv_path=csv_path, limit=universe_limit, rotation_key=rotation_key)
    liquid  = filter_universe(symbols, min_price=5.0,
                              min_avg_volume=1_000_000,
                              min_avg_dollar_volume=20_000_000)
    print(f"[Bounce Scanner] BEAR regime — scanning {len(liquid)} liquid symbols for capitulation\n")

    results = []
    for ticker in liquid:
        s = scan_ticker(ticker)
        if s and s["setup_score"] >= min_score:
            results.append(s)
            print(f"[Bounce Scanner] ✅ {ticker} | bounce | score {s['setup_score']} | "
                  f"gap {s['event_move']:+.1f}% {s['days_since']}d ago | {s['conviction']}")

    results.sort(key=lambda s: s["setup_score"], reverse=True)
    print(f"\n[Bounce Scanner] {len(results)} capitulation-bounce setups\n")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Capitulation-bounce scanner — Strategy 5")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--min-score", type=int, default=MIN_SETUP_SCORE)
    args = parser.parse_args()
    run_bounce_scan(universe_limit=args.limit, min_score=args.min_score)
