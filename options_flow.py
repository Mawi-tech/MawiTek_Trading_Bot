"""
options_flow.py

Detects unusual call activity using volume-to-open-interest (V/OI) ratio
combined with notional premium estimation.

A high V/OI ratio (default 0.5+) indicates aggressive call buying relative
to existing positioning — often a precursor to a directional move.

Data source: Tradier options chain + quote (replaces the old yfinance approach).
No extra API key required — uses the same Tradier credentials as the broker.

Public API (stable for options_scanner.py):
- has_bullish_flow(ticker, min_premium) -> bool
- get_bullish_sweep_tickers(min_premium, limit, symbols=None) -> list[dict]
"""

from tradier_client import get_options_expirations, get_options_chain, get_quote
from market_data import chain_to_df
from logger import get_logger

log = get_logger("options_flow")


# ─── Config ────────────────────────────────────────────────────────────────────

# Minimum V/OI ratio for "unusual" call activity
MIN_VOI_RATIO = 0.5

# How many upcoming expirations to scan per ticker (more = slower)
EXPIRIES_TO_SCAN = 2

# Limit calls to strikes within +/- this percent of spot (focus on ATM-ish flow)
ATM_LOWER_PCT = 0.10
ATM_UPPER_PCT = 0.15


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _estimate_call_premium(calls_df, spot_price: float) -> float:
    """
    Estimate notional premium of recent ATM-ish call activity:
    sum(volume * mid_price * 100) for strikes near the money.
    """
    if calls_df is None or calls_df.empty:
        return 0.0

    low  = spot_price * (1 - ATM_LOWER_PCT)
    high = spot_price * (1 + ATM_UPPER_PCT)
    atm  = calls_df[
        (calls_df["strike"] >= low) & (calls_df["strike"] <= high)
    ]

    if atm.empty:
        return 0.0

    bid  = atm["bid"].fillna(0)
    ask  = atm["ask"].fillna(0)
    last = atm["lastPrice"].fillna(0)

    # Mid where bid+ask available, else fall back to lastPrice
    mid = (bid + ask) / 2
    mid = mid.where(mid > 0, last)

    volume   = atm["volume"].fillna(0)
    notional = (volume * mid * 100).sum()
    return float(notional)


# ─── Public API ────────────────────────────────────────────────────────────────

def has_bullish_flow(ticker: str, min_premium: float = 50_000) -> bool:
    """
    Per-ticker bullish flow check via Tradier.

    Returns True when BOTH conditions are met:
      - Total call volume / open interest >= MIN_VOI_RATIO
      - Estimated ATM call notional premium >= min_premium

    Scans the next EXPIRIES_TO_SCAN expirations and aggregates.
    """
    try:
        # Spot price from Tradier quote
        spot = get_quote(ticker)
        if not spot or spot <= 0:
            return False

        # Expirations from Tradier
        expiries = get_options_expirations(ticker)[:EXPIRIES_TO_SCAN]
        if not expiries:
            return False

        total_volume  = 0.0
        total_oi      = 0.0
        total_premium = 0.0

        for exp in expiries:
            chain = get_options_chain(ticker, exp)
            if not chain:
                continue

            # Convert Tradier chain list → DataFrame with yfinance-compatible columns
            calls = chain_to_df(chain, option_type="call")
            if calls.empty:
                continue

            total_volume  += float(calls["volume"].fillna(0).sum())
            total_oi      += float(calls["openInterest"].fillna(0).sum())
            total_premium += _estimate_call_premium(calls, spot)

        if total_oi <= 0:
            return False

        voi_ratio = total_volume / total_oi
        result    = (voi_ratio >= MIN_VOI_RATIO) and (total_premium >= min_premium)

        log.debug("%s | V/OI: %.2f | Premium: $%.0f | Bullish: %s",
                  ticker, voi_ratio, total_premium, result)
        return result

    except Exception as e:
        log.warning("has_bullish_flow(%s): %s", ticker, e)
        return False


def get_bullish_sweep_tickers(
    min_premium: float = 50_000,
    limit: int = 100,
    symbols: list[str] | None = None,
) -> list[dict]:
    """
    Batch-scan helper. yfinance has no broad-market flow endpoint, so:

      - When called WITHOUT a symbols list (the default in options_scanner.py),
        returns []. The scanner will then run per-ticker has_bullish_flow()
        checks for each setup it's already considering — this avoids
        duplicate work.

      - When called WITH a symbols list, scans each one and returns
        those that pass has_bullish_flow().

    Returns: [{ticker, total_premium (placeholder), flow_count}, ...]
    """
    if not symbols:
        return []

    results = []
    for ticker in symbols[:limit]:
        if has_bullish_flow(ticker, min_premium=min_premium):
            results.append({
                "ticker": ticker,
                "total_premium": float(min_premium),  # placeholder; real value already logged
                "flow_count": 1,
            })
    return results


# ─── CLI Test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    test_tickers = sys.argv[1:] or ["NVDA", "AMD", "TSLA", "PLTR", "HOOD"]
    print(f"\n=== Testing bullish flow on: {', '.join(test_tickers)} ===\n")
    for t in test_tickers:
        has_bullish_flow(t, min_premium=50_000)
