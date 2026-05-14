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


# ─── Account ───────────────────────────────────────────────────────────────────

def get_account_balance() -> dict:
    """
    Returns account balances including total equity, cash, and option
    buying power. In MOCK_MODE returns a flat $10K account.
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

        return {
            "total_equity":       float(balances.get("total_equity", 0)),
            "option_buying_power": float(balances.get("option_short_value", 0)
                                         or balances.get("net_value", 0)),
            "cash":               float(balances.get("cash", {}).get("cash_available", 0)),
        }
    except Exception as e:
        print(f"[Tradier] Error fetching balance: {e}")
        return {"total_equity": 0, "option_buying_power": 0, "cash": 0}


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


def get_open_positions() -> list[dict]:
    """All current open positions. In MOCK_MODE returns []."""
    if MOCK_MODE:
        return []

    url = f"{BASE_URL}/accounts/{TRADIER_ACCOUNT_ID}/positions"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return _safe_inner(r.json(), "positions", "position")
    except Exception as e:
        print(f"[Tradier] Error fetching positions: {e}")
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


def get_quote(ticker: str) -> float:
    """Last price for a stock. MOCK_MODE returns 0.0."""
    if MOCK_MODE:
        return 0.0

    url = f"{BASE_URL}/markets/quotes"
    params = {"symbols": ticker}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        quote = data.get("quotes", {}).get("quote", {})
        return float(quote.get("last", 0))
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
    duration: str = "day"
) -> dict:
    """Place an options order. In MOCK_MODE refuses politely."""
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
