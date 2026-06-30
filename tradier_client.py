"""
tradier_client.py

Tradier API wrapper for options trading.
Handles authentication, account data, options chain,
and order placement/management.

Includes a MOCK_MODE guard: when TRADIER_API_KEY or TRADIER_ACCOUNT_ID
is unset, account/order calls return safe empty/default values instead
of making (and failing with 401) HTTP requests. This lets the bot run
in scan-only mode without credentials.

To enable real Tradier access, set in .env:
  TRADIER_API_KEY=<your_key>
  TRADIER_ACCOUNT_ID=<your_account_id>
  TRADIER_SANDBOX=true   # false for live

Docs: https://documentation.tradier.com
"""

import os
import requests
from datetime import datetime

# ─── Auto-load .env so any entry point gets credentials ────────────────────────
# We do this at import time, BEFORE reading any TRADIER_* vars, so every
# script that touches the broker (executor, hft_executor, dashboard_state,
# sandbox_validator) picks up the same credentials without having to call
# load_dotenv() themselves.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
except ImportError:
    pass   # python-dotenv not installed — fall back to OS env vars only


# ─── Config ────────────────────────────────────────────────────────────────────

TRADIER_API_KEY    = os.getenv("TRADIER_API_KEY", "")
TRADIER_ACCOUNT_ID = os.getenv("TRADIER_ACCOUNT_ID", "")

# Sandbox for testing, production for live
TRADIER_SANDBOX    = os.getenv("TRADIER_SANDBOX", "true").lower() == "true"

# If credentials are missing, run in MOCK_MODE — the rest of the bot
# can still scan, write dashboard state, and exercise its risk-management
# math against a dummy $10K equity.
MOCK_MODE = (not TRADIER_API_KEY) or (not TRADIER_ACCOUNT_ID)

if MOCK_MODE:
    print(
        "[Tradier] No API key/account configured — running in MOCK_MODE. "
        "Account & order calls will return safe defaults. "
        "Set TRADIER_API_KEY and TRADIER_ACCOUNT_ID in .env to enable live calls."
    )

BASE_URL = (
    "https://sandbox.tradier.com/v1"
    if TRADIER_SANDBOX
    else "https://api.tradier.com/v1"
)

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json",
}

# Default mock equity used for risk sizing when no broker is connected
MOCK_EQUITY = 10_000.0


class BrokerReadError(Exception):
    """
    A broker read genuinely FAILED (network / HTTP / parse / empty-where-
    impossible), as opposed to succeeding with a legitimately empty result.

    Most read helpers return a safe default ([] / 0) on error so display and
    counting callers degrade gracefully. But some callers MUST NOT act on a
    false "empty" — e.g. position reconciliation, which would journal every
    open position as "closed_externally" and stop managing it if a transient
    failure looked like "no positions". Those callers pass strict=True and
    catch this exception to bail out (no-op) instead. See the 2026-06 incident
    where a failed balance read poisoned the equity curve.
    """


# ─── Account ───────────────────────────────────────────────────────────────────

def get_account_balance(strict: bool = False) -> dict:
    """
    Returns account balances including total equity, cash, and option
    buying power. In MOCK_MODE returns a flat $10K account.

    strict=False (default): returns all-zeros on a failed read (back-compat;
      callers already guard equity <= 0 by failing closed).
    strict=True: raises BrokerReadError on a failed read OR a parsed-but-
      unusable response (total_equity <= 0). Use when a wrong equity number
      would corrupt persisted state (equity curve, daily-P&L baseline).
    """
    if MOCK_MODE:
        return {
            "total_equity":       MOCK_EQUITY,
            "option_buying_power": MOCK_EQUITY,
            "cash":               MOCK_EQUITY,
        }

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/balances"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        balances = data.get("balances", {})

        result = {
            "total_equity":       float(balances.get("total_equity", 0)),
            "option_buying_power": float(balances.get("option_short_value", 0)
                                         or balances.get("net_value", 0)),
            "cash":               float(balances.get("cash", {}).get("cash_available", 0)),
        }
    except Exception as e:
        print(f"[Tradier] Error fetching balance: {e}")
        if strict:
            raise BrokerReadError(f"balance read failed: {e}") from e
        return {"total_equity": 0, "option_buying_power": 0, "cash": 0}

    # A 200 that parses but reports no equity is still unusable (a partial /
    # malformed balances payload). Treat it as a failed read in strict mode.
    if strict and result["total_equity"] <= 0:
        raise BrokerReadError(
            f"balance read returned unusable total_equity={result['total_equity']}"
        )
    return result


def _safe_inner(container, outer_key: str, inner_key: str) -> list:
    """
    Tradier sometimes returns the literal string "null" (or null) when an
    inner collection is empty — e.g. `"orders": "null"` or `"orders": null`.
    Defensively unwrap to always return a list of dicts.
    """
    outer = container.get(outer_key, {})
    if not isinstance(outer, dict):
        return []
    inner = outer.get(inner_key, [])
    if inner in (None, "null", ""):
        return []
    if isinstance(inner, dict):
        return [inner]
    if isinstance(inner, list):
        return inner
    return []


def get_open_positions(strict: bool = False) -> list[dict]:
    """
    All current open positions. In MOCK_MODE returns [].

    strict=False (default): returns [] on a failed read (back-compat; safe for
      display / counting callers that self-correct on the next cycle).
    strict=True: raises BrokerReadError on a failed read, so reconciliation
      never mistakes a transient API failure for "no positions" and journals
      every still-open position as closed_externally. A genuinely-empty
      response (account really holds nothing) still returns [] — only a
      failure raises.
    """
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/positions"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return _safe_inner(r.json(), "positions", "position")
    except Exception as e:
        print(f"[Tradier] Error fetching positions: {e}")
        if strict:
            raise BrokerReadError(f"positions read failed: {e}") from e
        return []


def get_orders_today() -> list[dict]:
    """Orders placed today. In MOCK_MODE returns []."""
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        orders = _safe_inner(r.json(), "orders", "order")
        today  = datetime.now().strftime("%Y-%m-%d")
        return [
            o for o in orders
            if isinstance(o, dict) and str(o.get("transaction_date", "")).startswith(today)
        ]
    except Exception as e:
        print(f"[Tradier] Error fetching orders: {e}")
        return []


# Order statuses that mean the order is still working at the broker.
_WORKING_ORDER_STATUSES = {"open", "pending", "partially_filled", "submitted", "accepted", "calculated", "queued"}


def get_open_orders() -> list[dict]:
    """All currently working (cancelable) orders. MOCK_MODE returns []."""
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        orders = _safe_inner(r.json(), "orders", "order")
        return [
            o for o in orders
            if isinstance(o, dict) and str(o.get("status", "")).lower() in _WORKING_ORDER_STATUSES
        ]
    except Exception as e:
        print(f"[Tradier] Error fetching open orders: {e}")
        return []


# ─── Options Chain ─────────────────────────────────────────────────────────────

def get_options_expirations(ticker: str) -> list[str]:
    """Available expiration dates for a ticker. MOCK_MODE returns []."""
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/markets/options/expirations"
    params = {"symbol": ticker, "includeAllRoots": "true"}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        dates = data.get("expirations", {}).get("date", [])
        return dates if isinstance(dates, list) else [dates]
    except Exception as e:
        print(f"[Tradier] Error fetching expirations for {ticker}: {e}")
        return []


def get_options_chain(ticker: str, expiration: str) -> list[dict]:
    """Full options chain. MOCK_MODE returns []."""
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/markets/options/chains"
    params = {
        "symbol":     ticker,
        "expiration": expiration,
        "greeks":     "true",
    }
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        options = data.get("options", {}).get("option", [])
        return options if isinstance(options, list) else [options]
    except Exception as e:
        print(f"[Tradier] Error fetching chain for {ticker} {expiration}: {e}")
        return []


def get_option_mid(option_symbol: str, underlying: str, expiration: str) -> float:
    """
    Live mid price for one option contract, looked up from its chain.

    Returns (bid + ask) / 2 when both sides quote; falls back to whichever
    single side is available; 0.0 if the contract can't be priced. This is the
    one place option mid-pricing lives — position_manager, equity_tracker,
    dashboard_state and iv_rank_bot all use it.
    """
    try:
        for contract in get_options_chain(underlying, expiration):
            if contract.get("symbol") == option_symbol:
                bid = float(contract.get("bid", 0) or 0)
                ask = float(contract.get("ask", 0) or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
                return ask or bid or 0.0
    except Exception as e:
        print(f"[Tradier] Error pricing {option_symbol}: {e}")
    return 0.0


def get_quotes(symbols: list[str]) -> dict[str, float]:
    """
    Last price for MANY stocks in one call (Tradier /markets/quotes takes a
    comma-separated list). Returns {symbol: price}; symbols that can't be priced
    are omitted. MOCK_MODE returns {}. Chunked so a long list can't blow the URL.
    """
    out: dict[str, float] = {}
    if MOCK_MODE or not symbols:
        return out

    url = f"{BASE_URL}/markets/quotes"
    CHUNK = 100
    for i in range(0, len(symbols), CHUNK):
        batch = symbols[i:i + CHUNK]
        try:
            r = requests.get(url, headers=HEADERS,
                             params={"symbols": ",".join(batch)}, timeout=10)
            r.raise_for_status()
            quotes = r.json().get("quotes", {}).get("quote", [])
            if isinstance(quotes, dict):
                quotes = [quotes]
            for q in quotes:
                sym = q.get("symbol")
                if not sym:
                    continue
                for field in ("last", "close", "prevclose", "bid", "ask"):
                    val = q.get(field)
                    if val not in (None, "", 0, "0"):
                        try:
                            out[sym] = float(val)
                            break
                        except (TypeError, ValueError):
                            continue
        except Exception as e:
            print(f"[Tradier] Error fetching quotes batch: {e}")
    return out


def get_chain_greeks(underlying: str, expiration: str) -> dict[str, dict]:
    """
    Per-contract greeks for a whole expiration, keyed by OCC option symbol.

    Tradier returns greeks (delta/gamma/theta/vega/rho + mid_iv, via ORATS) on
    each contract when the chain is requested with greeks=true — which
    get_options_chain already does. This fetches the chain ONCE and maps each
    symbol to its greeks, so the portfolio-greeks aggregation can price a whole
    underlying+expiry group with a single API call.

    Returns {symbol: {"delta","gamma","theta","vega","iv"}}. MOCK_MODE → {}.
    Contracts with no greeks block (illiquid / unpriced) are skipped.
    """
    out: dict[str, dict] = {}
    if MOCK_MODE:
        return out
    try:
        for contract in get_options_chain(underlying, expiration):
            sym = contract.get("symbol")
            g = contract.get("greeks") or {}
            if not sym or not g:
                continue
            out[sym] = {
                "delta": float(g.get("delta", 0) or 0),
                "gamma": float(g.get("gamma", 0) or 0),
                "theta": float(g.get("theta", 0) or 0),
                "vega":  float(g.get("vega", 0) or 0),
                "iv":    float(g.get("mid_iv", 0) or 0),
            }
    except Exception as e:
        print(f"[Tradier] Error fetching greeks for {underlying} {expiration}: {e}")
    return out


def get_quote(ticker: str) -> float:
    """
    Last price for a stock. MOCK_MODE returns 0.0.
    Tradier sometimes returns "last": null (no trade since open / closed market) —
    fall back to the prevclose so callers always get a usable number.
    """
    if MOCK_MODE:
        return 0.0

    url = f"{BASE_URL}/markets/quotes"
    params = {"symbols": ticker}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        quote = data.get("quotes", {}).get("quote", {})
        if not isinstance(quote, dict):
            return 0.0
        for field in ("last", "close", "prevclose", "bid", "ask"):
            val = quote.get(field)
            if val not in (None, "", 0, "0"):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 0.0
    except Exception as e:
        print(f"[Tradier] Error fetching quote for {ticker}: {e}")
        return 0.0


# ─── Order Placement ───────────────────────────────────────────────────────────

def place_option_order(
    symbol: str,
    option_symbol: str,
    side: str,
    quantity: int,
    order_type: str = "market",
    price: float | None = None,
    duration: str = "day",
    tag: str | None = None,
) -> dict:
    """
    Place an options order. In MOCK_MODE refuses politely.

    `tag` is an optional client-side identifier (alphanumeric, max 255 chars)
    echoed back by Tradier on the order record. Used for idempotency — the
    bot can scan open/recent orders for a tag to detect whether an order it
    tried to place actually went through after a crash.
    """
    if MOCK_MODE:
        msg = "MOCK_MODE: cannot place real orders without TRADIER_API_KEY"
        print(f"[Tradier] {msg} | Would have placed: {side} {quantity}x {option_symbol}")
        return {"status": "error", "error": msg}

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders"

    payload = {
        "class":    "option",
        "symbol":   symbol,
        "option_symbol": option_symbol,
        "side":     side,
        "quantity": str(quantity),
        "type":     order_type,
        "duration": duration,
    }

    if order_type == "limit" and price:
        payload["price"] = str(round(price, 2))

    if tag:
        # Tradier only accepts [A-Za-z0-9-] in tags; sanitize defensively.
        clean = "".join(c for c in tag if c.isalnum() or c == "-")[:255]
        if clean:
            payload["tag"] = clean

    try:
        r = requests.post(url, headers=HEADERS, data=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        order = data.get("order", {})

        print(
            f"[Tradier] Order placed | {side} {quantity}x {option_symbol} | "
            f"Type: {order_type} | Status: {order.get('status')} | "
            f"ID: {order.get('id')}"
        )
        return order

    except requests.exceptions.HTTPError as e:
        print(f"[Tradier] HTTP error placing order: {e} | Response: {r.text}")
        return {"status": "error", "error": str(e)}
    except Exception as e:
        print(f"[Tradier] Error placing order: {e}")
        return {"status": "error", "error": str(e)}


def get_order_status(order_id: str | int) -> dict:
    """
    Fetch the current state of a single order by ID.

    Returns the Tradier order dict, which includes:
        status            "open" | "filled" | "partially_filled" |
                          "pending" | "rejected" | "canceled" | "expired"
        avg_fill_price    average fill price across executions
        exec_quantity     contracts filled so far
        remaining_quantity contracts still working
        last_fill_price / last_fill_quantity

    On error or in MOCK_MODE returns {"status": "error", "error": ...}.
    """
    if MOCK_MODE:
        return {"status": "error", "error": "MOCK_MODE: no live orders"}

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders/{order_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        order = data.get("order", {})
        return order if isinstance(order, dict) else {"status": "error", "error": "unexpected response"}
    except Exception as e:
        print(f"[Tradier] Error fetching order {order_id}: {e}")
        return {"status": "error", "error": str(e)}


def find_orders_by_tag(tag: str) -> list[dict]:
    """
    Return all orders (open or historical for the session) carrying a given
    client tag. Used for idempotency — after a crash the bot can check
    whether the order it was about to place already exists at the broker.
    """
    if MOCK_MODE or not tag:
        return []

    clean = "".join(c for c in tag if c.isalnum() or c == "-")[:255]
    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        orders = _safe_inner(r.json(), "orders", "order")
        return [o for o in orders if isinstance(o, dict) and str(o.get("tag", "")) == clean]
    except Exception as e:
        print(f"[Tradier] Error searching orders by tag: {e}")
        return []


def cancel_order(order_id: str) -> bool:
    """Cancel an open order by ID. MOCK_MODE returns False."""
    if MOCK_MODE:
        return False

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/orders/{order_id}"
    try:
        r = requests.delete(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        print(f"[Tradier] Cancelled order {order_id}")
        return True
    except Exception as e:
        print(f"[Tradier] Error cancelling order {order_id}: {e}")
        return False


# ─── Gain/Loss History ────────────────────────────────────────────────────────

def get_gainloss() -> list[dict]:
    """
    Fetch closed-position gain/loss records from the broker.
    Each item represents a closed lot with entry/exit data and P&L.
    Used to populate the Trade History tab when the local journal is empty.

    MOCK_MODE returns [].
    """
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/gainloss"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return _safe_inner(r.json(), "gainloss", "closed_position")
    except Exception as e:
        print(f"[Tradier] Error fetching gain/loss: {e}")
        return []
