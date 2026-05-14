"""
option_selector.py

Intelligently selects the best expiration and strike
for a long call position based on:

- Days until earnings (want expiry AFTER earnings)
- Delta targeting (0.40-0.55 for balanced leverage)
- Bid/ask spread quality (liquidity check)
- Premium budget from risk sizing

Bot logic:
- Expiry: Pick the first expiration that is at least 2 days
  AFTER the earnings date, with preference for 7-21 DTE total
- Strike: Target delta ~0.45-0.55 (near ATM) unless premium
  is too high, then step out to ~0.35 delta
"""

from datetime import datetime, timedelta
from tradier_client import get_options_expirations, get_options_chain, get_quote


# ─── Config ────────────────────────────────────────────────────────────────────

TARGET_DELTA_MIN  = 0.35
TARGET_DELTA_MAX  = 0.60
FALLBACK_DELTA_MIN = 0.25   # If nothing in main range
MAX_SPREAD_PCT    = 0.15    # Max bid/ask spread as % of mid (15%)
MIN_OPEN_INTEREST = 50      # Minimum OI for liquidity
MIN_DTE           = 5       # Minimum days to expiry
MAX_DTE           = 45      # Maximum days to expiry
# Freshly-listed weekly expiries have 0 OI but real volume, producing
# misleading V/OI ratios and often wide spreads. Skip contracts where
# volume/OI exceeds this multiple.
MAX_VOI_RATIO     = 10.0


# ─── Helpers ───────────────────────────────────────────────────────────────────

def parse_expiry(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def days_to_expiry(date_str: str) -> int:
    return (parse_expiry(date_str) - datetime.now()).days


def spread_quality(bid: float, ask: float) -> float:
    """Returns spread as % of mid price. Lower = better."""
    mid = (bid + ask) / 2
    if mid <= 0:
        return 1.0
    return (ask - bid) / mid


def score_option(contract: dict, budget_per_contract: float) -> float:
    """
    Score an individual option contract.
    Higher = better.

    Factors:
    - Delta closeness to 0.50
    - Spread quality
    - Open interest
    - Premium vs budget fit
    """
    try:
        greeks  = contract.get("greeks") or {}
        delta   = abs(float(greeks.get("delta", 0) or 0))
        bid     = float(contract.get("bid", 0) or 0)
        ask     = float(contract.get("ask", 0) or 0)
        oi      = int(contract.get("open_interest", 0) or 0)
        mid     = (bid + ask) / 2

        if bid <= 0 or ask <= 0 or mid <= 0:
            return -1.0

        # Delta score — reward proximity to 0.50
        delta_score = 1.0 - abs(delta - 0.50) * 2

        # Spread score — lower spread % = better
        spread = spread_quality(bid, ask)
        spread_score = max(0, 1.0 - spread / MAX_SPREAD_PCT)

        # OI score
        oi_score = min(1.0, oi / 500)

        # Budget fit — prefer contracts near but under budget
        premium_total = mid * 100  # 1 contract = 100 shares
        if premium_total > budget_per_contract * 1.2:
            budget_score = 0.0       # Too expensive
        elif premium_total > budget_per_contract:
            budget_score = 0.5
        else:
            budget_score = 1.0 - abs(premium_total - budget_per_contract) / budget_per_contract

        total = (
            delta_score  * 0.35 +
            spread_score * 0.30 +
            oi_score     * 0.15 +
            budget_score * 0.20
        )

        return round(total, 4)

    except Exception:
        return -1.0


# ─── Main Selector ─────────────────────────────────────────────────────────────

def select_option(
    ticker: str,
    days_until_earnings: int | None,
    budget_per_contract: float,
) -> dict | None:
    """
    Select the best call option for a catalyst-driven long play.

    Args:
        ticker:               Underlying symbol
        days_until_earnings:  Days until earnings (None if unknown)
        budget_per_contract:  Max $ per contract based on risk sizing

    Returns:
        Best option contract dict or None if nothing qualifies.
    """
    expirations = get_options_expirations(ticker)
    if not expirations:
        print(f"[Selector] No expirations found for {ticker}")
        return None

    stock_price = get_quote(ticker)
    if stock_price <= 0:
        print(f"[Selector] Could not get quote for {ticker}")
        return None

    # Filter expirations to valid DTE range
    # If earnings known, ensure expiry is at least 2 days AFTER earnings
    valid_expiries = []
    for exp in expirations:
        dte = days_to_expiry(exp)
        if dte < MIN_DTE or dte > MAX_DTE:
            continue
        if days_until_earnings is not None and dte < days_until_earnings + 2:
            continue  # Expiry before or same day as earnings — skip
        valid_expiries.append(exp)

    if not valid_expiries:
        print(f"[Selector] No valid expirations for {ticker} in DTE range")
        return None

    print(f"[Selector] {ticker} | Stock: ${stock_price:.2f} | "
          f"Budget/contract: ${budget_per_contract:.0f} | "
          f"Checking {len(valid_expiries)} expirations")

    best_contract  = None
    best_score     = -1.0

    for exp in valid_expiries:
        chain = get_options_chain(ticker, exp)

        # Filter to calls only
        calls = [c for c in chain if c.get("option_type") == "call"]

        for contract in calls:
            try:
                greeks  = contract.get("greeks") or {}
                delta   = abs(float(greeks.get("delta", 0) or 0))
                oi      = int(contract.get("open_interest", 0) or 0)
                volume  = int(contract.get("volume", 0) or 0)
                bid     = float(contract.get("bid", 0) or 0)
                ask     = float(contract.get("ask", 0) or 0)

                # Hard filters
                if delta < TARGET_DELTA_MIN or delta > TARGET_DELTA_MAX:
                    continue
                if oi < MIN_OPEN_INTEREST:
                    continue
                if bid <= 0 or ask <= 0:
                    continue
                if spread_quality(bid, ask) > MAX_SPREAD_PCT:
                    continue
                # Skip freshly-listed weekly expiries: high volume against
                # near-zero OI produces anomalous V/OI ratios and unreliable
                # greeks. Only check when OI > 0 to avoid division by zero.
                if oi > 0 and volume / oi > MAX_VOI_RATIO:
                    continue

                score = score_option(contract, budget_per_contract)
                if score > best_score:
                    best_score    = score
                    best_contract = contract
                    best_contract["_expiration"] = exp
                    best_contract["_dte"]        = days_to_expiry(exp)
                    best_contract["_score"]      = score
                    best_contract["_mid_price"]  = round((bid + ask) / 2, 2)

            except Exception:
                continue

    if best_contract:
        print(
            f"[Selector] ✅ Best contract: {best_contract.get('symbol')} | "
            f"Strike: {best_contract.get('strike')} | "
            f"Exp: {best_contract.get('_expiration')} | "
            f"DTE: {best_contract.get('_dte')} | "
            f"Mid: ${best_contract.get('_mid_price')} | "
            f"Score: {best_contract.get('_score'):.3f}"
        )
    else:
        print(f"[Selector] ❌ No qualifying contract found for {ticker}")

    return best_contract
