"""
portfolio_greeks.py — net option greeks across the whole open book.

An options book's real risk isn't captured by position counts or dollar P&L —
it's the aggregate greeks. This module sums delta / gamma / theta / vega across
every open leg (longs add, shorts subtract) so the dashboard can show the book's
directional and volatility exposure, and the risk manager can cap net vega.

Conventions (all in DOLLAR terms, contract multiplier 100 applied):
  • net_delta — $ P&L per +$1 in the underlying (share-equivalents = net_delta/spot)
  • net_gamma — change in net_delta per +$1 in the underlying
  • net_theta — $ P&L per calendar day from time decay (longs bleed, shorts earn)
  • net_vega  — $ P&L per +1 implied-vol POINT (positive = long vol, negative = short vol)

Greeks come from Tradier (ORATS) via tradier_client.get_chain_greeks — one chain
fetch per underlying+expiry group. The computed totals are cached to
portfolio_greeks.json so the risk manager can read net vega cheaply in the
pre-trade hot path without re-fetching.
"""

from mawitek.infra.state_io import atomic_write_json, read_json
from mawitek.infra.utils import now_est

GREEKS_CACHE_FILE = "portfolio_greeks.json"
CONTRACT_MULTIPLIER = 100


def aggregate_greeks(legs: list[dict]) -> dict:
    """
    Sum per-leg greeks into net book greeks. PURE — no I/O.

    Each leg: {"delta","gamma","theta","vega","quantity"} where quantity is
    signed (long > 0, short < 0). Per-leg dollar greek = greek × quantity × 100.

    Returns net_delta/gamma/theta/vega plus gross_vega (Σ |leg vega|, a size
    gauge that doesn't net long against short) and leg_count.
    """
    net = {"net_delta": 0.0, "net_gamma": 0.0, "net_theta": 0.0, "net_vega": 0.0}
    gross_vega = 0.0
    counted = 0
    for leg in legs:
        qty = leg.get("quantity", 0) or 0
        if not qty:
            continue
        mult = qty * CONTRACT_MULTIPLIER
        net["net_delta"] += float(leg.get("delta", 0) or 0) * mult
        net["net_gamma"] += float(leg.get("gamma", 0) or 0) * mult
        net["net_theta"] += float(leg.get("theta", 0) or 0) * mult
        leg_vega = float(leg.get("vega", 0) or 0) * mult
        net["net_vega"] += leg_vega
        gross_vega += abs(leg_vega)
        counted += 1

    return {
        "net_delta": round(net["net_delta"], 2),
        "net_gamma": round(net["net_gamma"], 4),
        "net_theta": round(net["net_theta"], 2),
        "net_vega":  round(net["net_vega"], 2),
        "gross_vega": round(gross_vega, 2),
        "leg_count": counted,
    }


def compute_portfolio_greeks(positions: list[dict]) -> dict:
    """
    Fetch live greeks for every leg of `positions` (the build_positions_data
    output) and aggregate them. Persists the result to the cache for the risk
    manager. Returns the aggregate dict (greeks all 0 if nothing priced — e.g.
    MOCK_MODE or an empty book), with a `priced` flag indicating whether any
    leg actually had greeks (so callers can distinguish "flat" from "no data").
    """
    from mawitek.data.tradier_client import get_chain_greeks

    # One chain fetch per (underlying, expiration) — share it across that group's legs.
    chain_cache: dict[tuple, dict] = {}
    enriched: list[dict] = []
    priced = False

    for pos in positions or []:
        underlying = pos.get("underlying", "")
        expiration = pos.get("expiration", "")
        key = (underlying, expiration)
        if key not in chain_cache:
            chain_cache[key] = get_chain_greeks(underlying, expiration)
        greeks_by_sym = chain_cache[key]

        for leg in pos.get("legs", []):
            g = greeks_by_sym.get(leg.get("symbol"))
            if not g:
                continue
            priced = True
            enriched.append({**g, "quantity": leg.get("quantity", 0)})

    agg = aggregate_greeks(enriched)
    agg["priced"] = priced
    agg["updated"] = now_est().isoformat()

    try:
        atomic_write_json(GREEKS_CACHE_FILE, agg)
    except Exception as e:
        print(f"[Greeks] Could not persist portfolio greeks: {e}")

    return agg


def read_cached_greeks() -> dict | None:
    """Last computed portfolio greeks (for the risk manager). None if missing."""
    data = read_json(GREEKS_CACHE_FILE, None)
    return data if isinstance(data, dict) else None


def cached_net_vega() -> float | None:
    """
    Net dollar vega from the cache, or None when unavailable (no cache, or the
    book was never actually priced). Returning None lets the risk manager FAIL
    OPEN — a missing greek read must never block trading.
    """
    g = read_cached_greeks()
    if not g or not g.get("priced"):
        return None
    try:
        return float(g.get("net_vega"))
    except (TypeError, ValueError):
        return None
