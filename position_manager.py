"""
position_manager.py

Tracks open option positions and manages exits:

- 100% gain → take profit (double your money)
- 50% loss  → stop loss (cut losers fast on options)
- 1 day before expiry → force close (avoid expiry risk)
- Post-earnings → close next morning (catalyst played out)

All exits go through Tradier sell_to_close orders.
"""

import json
import os
import datetime
from tradier_client import (
    get_open_positions,
    get_options_chain,
    place_option_order,
    get_quote,
)


# ─── Exit Config ───────────────────────────────────────────────────────────────

TAKE_PROFIT_PCT   = 1.00    # Close at +100% (2x your money)
STOP_LOSS_PCT     = -0.50   # Close at -50% (standard for long options)
FORCE_CLOSE_DTE   = 1       # Force close with 1 day left to avoid pin risk

# File to persist entry prices across restarts
POSITIONS_FILE = "open_positions.json"


# ─── Position Storage ──────────────────────────────────────────────────────────

def load_positions() -> dict:
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_positions(positions: dict):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def record_entry(
    option_symbol: str,
    underlying: str,
    entry_price: float,
    quantity: int,
    expiration: str,
    earnings_date: str | None = None
):
    """Record a new position entry."""
    positions = load_positions()
    positions[option_symbol] = {
        "underlying":   underlying,
        "entry_price":  entry_price,
        "quantity":     quantity,
        "expiration":   expiration,
        "earnings_date": earnings_date,
        "entry_time":   datetime.datetime.now().isoformat(),
    }
    save_positions(positions)
    print(f"[PositionManager] Recorded entry: {option_symbol} @ ${entry_price:.2f} x{quantity}")


def remove_position(option_symbol: str):
    positions = load_positions()
    if option_symbol in positions:
        del positions[option_symbol]
        save_positions(positions)


# ─── Current Price ─────────────────────────────────────────────────────────────

def get_option_mid(option_symbol: str, underlying: str, expiration: str) -> float:
    """
    Get current mid price for an option.
    Falls back to 0 if unavailable.
    """
    try:
        chain = get_options_chain(underlying, expiration)
        for contract in chain:
            if contract.get("symbol") == option_symbol:
                bid = float(contract.get("bid", 0) or 0)
                ask = float(contract.get("ask", 0) or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
    except Exception as e:
        print(f"[PositionManager] Error getting price for {option_symbol}: {e}")
    return 0.0


# ─── Exit Logic ────────────────────────────────────────────────────────────────

def days_until_expiry(expiration: str) -> int:
    try:
        exp_date = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp_date - datetime.date.today()).days
    except Exception:
        return 999


def earnings_have_passed(earnings_date: str | None) -> bool:
    if not earnings_date:
        return False
    try:
        edate = datetime.datetime.strptime(earnings_date, "%Y-%m-%d").date()
        return datetime.date.today() > edate
    except Exception:
        return False


def should_exit(
    option_symbol: str,
    entry_price: float,
    current_price: float,
    expiration: str,
    earnings_date: str | None
) -> tuple[bool, str]:
    """
    Determine if we should exit a position.

    Returns:
        (should_exit: bool, reason: str)
    """
    if current_price <= 0:
        return False, "No price available"

    pnl_pct = (current_price - entry_price) / entry_price

    # Take profit
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, f"Take profit hit (+{pnl_pct*100:.1f}%)"

    # Stop loss
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"Stop loss hit ({pnl_pct*100:.1f}%)"

    # Force close near expiry
    dte = days_until_expiry(expiration)
    if dte <= FORCE_CLOSE_DTE:
        return True, f"Force close — only {dte} DTE remaining"

    # Post-earnings close
    if earnings_have_passed(earnings_date):
        return True, "Earnings catalyst played out — closing position"

    return False, ""


# ─── Main Monitor Loop ─────────────────────────────────────────────────────────

def monitor_positions():
    """
    Check all open positions and exit those that hit
    take profit, stop loss, expiry, or post-earnings criteria.
    """
    positions = load_positions()

    if not positions:
        print("[PositionManager] No open positions to monitor.")
        return

    print(f"\n[PositionManager] Monitoring {len(positions)} open positions...\n")

    for option_symbol, data in list(positions.items()):
        underlying    = data["underlying"]
        entry_price   = float(data["entry_price"])
        quantity      = int(data["quantity"])
        expiration    = data["expiration"]
        earnings_date = data.get("earnings_date")

        current_price = get_option_mid(option_symbol, underlying, expiration)
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if current_price > 0 else 0

        print(
            f"[PositionManager] {option_symbol} | "
            f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
            f"P&L: {pnl_pct:+.1f}% | DTE: {days_until_expiry(expiration)}"
        )

        exit_now, reason = should_exit(
            option_symbol, entry_price, current_price, expiration, earnings_date
        )

        if exit_now:
            print(f"[PositionManager] 🚪 Exiting {option_symbol} — {reason}")

            order = place_option_order(
                symbol=underlying,
                option_symbol=option_symbol,
                side="sell_to_close",
                quantity=quantity,
                order_type="market",
            )

            if order.get("status") not in ("error", None):
                remove_position(option_symbol)
                print(f"[PositionManager] ✅ Closed {option_symbol} | Reason: {reason}")
            else:
                print(f"[PositionManager] ❌ Failed to close {option_symbol}: {order}")
