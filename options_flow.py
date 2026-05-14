"""
options_flow.py

Detects unusual call activity using yfinance volume-to-open-interest (V/OI)
ratio combined with notional premium estimation.

A high V/OI ratio (default 0.5+) indicates aggressive call buying relative
to existing positioning — often a precursor to a directional move.

Replaces the previous Unusual Whales API integration ($125/month) with
a free yfinance-based detection method. No API key required.

Public API (kept stable for options_scanner.py):
- has_bullish_flow(ticker, min_premium) -> bool
- get_bullish_sweep_tickers(min_premium, limit, symbols=None) -> list[dict]
"""

import yfinance as yf


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
    atm = calls_df[(calls_df["strike"] >= low) & (calls_df["strike"] <= high)]

    if atm.empty:
        return 0.0

    bid = atm["bid"].fillna(0)
    ask = atm["ask"].fillna(0)
    last = atm["lastPrice"].fillna(0)

    # Mid where bid+ask available, else fall back to lastPrice
    mid = (bid + ask) / 2
    mid = mid.where(mid > 0, last)

    volume = atm["volume"].fillna(0)
    notional = (volume * mid * 100).sum()
    return float(notional)


def _spot_price(tk: yf.Ticker) -> float | None:
    """Get the most recent close for the underlying."""
    try:
        hist = tk.history(period="2d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


# ─── Public API ────────────────────────────────────────────────────────────────

def has_bullish_flow(ticker: str, min_premium: float = 50_000) -> bool:
    """
    Per-ticker bullish flow check.

    Returns True when BOTH conditions are met:
      - Total call volume / open interest >= MIN_VOI_RATIO
      - Estimated ATM call notional premium >= min_premium

    Scans the next EXPIRIES_TO_SCAN expirations and aggregates.
    """
    try:
        tk = yf.Ticker(ticker)

        spot = _spot_price(tk)
        if not spot or spot <= 0:
            return False

        expiries = list(tk.options[:EXPIRIES_TO_SCAN]) if tk.options else []
        if not expiries:
            return False

        total_volume = 0.0
        total_oi     = 0.0
        total_premium = 0.0

        for exp in expiries:
            try:
                chain = tk.option_chain(exp)
            except Exception:
                continue

            calls = chain.calls
            if calls is None or calls.empty:
                continue

            total_volume  += float(calls["volume"].fillna(0).sum())
            total_oi      += float(calls["openInterest"].fillna(0).sum())
            total_premium += _estimate_call_premium(calls, spot)

        if total_oi <= 0:
            return False

        voi_ratio = total_volume / total_oi
        result = (voi_ratio >= MIN_VOI_RATIO) and (total_premium >= min_premium)

        print(
            f"[OptionsFlow] {ticker} | V/OI: {voi_ratio:.2f} | "
            f"Premium: ${total_premium:,.0f} | Bullish: {result}"
        )
        return result

    except Exception as e:
        print(f"[OptionsFlow] Error checking {ticker}: {e}")
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
