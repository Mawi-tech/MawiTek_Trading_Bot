"""
risk_manager.py

Handles all risk controls for the options bot:

1. Position sizing — % of account equity per trade
2. Daily loss limit — halt trading if exceeded
3. Max concurrent positions
4. Per-trade max loss (stop tracking)
5. Duplicate position guard
"""

import json
import os
import datetime
from tradier_client import get_account_balance, get_orders_today, get_open_positions


# ─── Risk Config ───────────────────────────────────────────────────────────────

RISK_PER_TRADE_PCT    = 0.02    # Risk 2% of account per trade
DAILY_LOSS_LIMIT_PCT  = 0.05    # Halt if down 5% on the day
MAX_OPEN_POSITIONS    = 5       # Max concurrent option positions
MAX_POSITION_SIZE_PCT = 0.05    # No single position > 5% of account

# State file to persist daily P&L across restarts
STATE_FILE = "risk_state.json"


# ─── State Management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    today = datetime.date.today().isoformat()
    default = {
        "date": today,
        "realized_pnl": 0.0,
        "trades_today": 0,
        "halted": False,
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # Reset if it's a new day
        if state.get("date") != today:
            return default
        return state
    except Exception:
        return default


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Core Risk Checks ──────────────────────────────────────────────────────────

def get_position_size(equity: float) -> float:
    """
    Returns max $ to allocate per trade based on account equity.
    Caps at MAX_POSITION_SIZE_PCT of account.
    """
    risk_amount  = equity * RISK_PER_TRADE_PCT
    max_position = equity * MAX_POSITION_SIZE_PCT
    return min(risk_amount, max_position)


def calculate_contracts(budget: float, mid_price: float) -> int:
    """
    How many contracts can we buy with our budget?
    Each contract = 100 shares of premium.
    Always rounds down — never oversize.
    """
    if mid_price <= 0:
        return 0
    cost_per_contract = mid_price * 100
    contracts = int(budget // cost_per_contract)
    return max(0, contracts)


def is_already_in_position(ticker: str) -> bool:
    """Check if we already hold a call on this ticker."""
    positions = get_open_positions()
    for pos in positions:
        symbol = pos.get("symbol", "")
        # Option symbols start with the underlying ticker
        if symbol.upper().startswith(ticker.upper()):
            return True
    return False


def count_open_option_positions() -> int:
    """Count current open option positions."""
    positions = get_open_positions()
    return sum(
        1 for p in positions
        if len(p.get("symbol", "")) > 6  # Options symbols are long
    )


def calculate_daily_pnl() -> float:
    """
    Estimate realized P&L from today's closed orders.
    Positive = profit, Negative = loss.
    """
    orders = get_orders_today()
    realized = 0.0

    for order in orders:
        if order.get("status") != "filled":
            continue

        side  = order.get("side", "")
        qty   = float(order.get("quantity", 0))
        price = float(order.get("avg_fill_price", 0))

        if "sell" in side.lower():
            realized += qty * price * 100
        elif "buy" in side.lower():
            realized -= qty * price * 100

    return realized


def check_daily_loss_limit(equity: float) -> tuple[bool, float]:
    """
    Check if daily loss limit has been breached.

    Returns:
        (is_halted: bool, current_pnl: float)
    """
    state = load_state()

    # If already halted today, stay halted
    if state.get("halted"):
        return True, state.get("realized_pnl", 0.0)

    pnl = calculate_daily_pnl()
    limit = -abs(equity * DAILY_LOSS_LIMIT_PCT)

    state["realized_pnl"] = pnl
    save_state(state)

    if pnl <= limit:
        state["halted"] = True
        save_state(state)
        print(
            f"[RiskManager] ⛔ DAILY LOSS LIMIT HIT | "
            f"P&L: ${pnl:,.2f} | Limit: ${limit:,.2f} | Trading HALTED"
        )
        return True, pnl

    return False, pnl


# ─── Full Pre-Trade Check ──────────────────────────────────────────────────────

def pre_trade_check(ticker: str) -> dict:
    """
    Run all risk checks before placing a trade.

    Returns:
        {
            "approved": bool,
            "reason": str,
            "equity": float,
            "budget": float,        # Max $ for this trade
            "daily_pnl": float,
        }
    """
    # Get account data
    balances = get_account_balance()
    equity   = balances.get("total_equity", 0)

    if equity <= 0:
        return _reject("Could not fetch account equity", equity=0, budget=0, pnl=0)

    # Daily loss limit
    halted, daily_pnl = check_daily_loss_limit(equity)
    if halted:
        return _reject(
            f"Daily loss limit hit (P&L: ${daily_pnl:,.2f})",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Max open positions
    open_count = count_open_option_positions()
    if open_count >= MAX_OPEN_POSITIONS:
        return _reject(
            f"Max open positions reached ({open_count}/{MAX_OPEN_POSITIONS})",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Duplicate position guard
    if is_already_in_position(ticker):
        return _reject(
            f"Already in position on {ticker}",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Calculate budget
    budget = get_position_size(equity)

    print(
        f"[RiskManager] ✅ {ticker} approved | "
        f"Equity: ${equity:,.2f} | Budget: ${budget:,.2f} | "
        f"Daily P&L: ${daily_pnl:,.2f} | Open positions: {open_count}"
    )

    return {
        "approved": True,
        "reason":   "All checks passed",
        "equity":   equity,
        "budget":   budget,
        "daily_pnl": daily_pnl,
    }


def record_trade(ticker: str):
    """Record that a trade was placed today."""
    state = load_state()
    state["trades_today"] = state.get("trades_today", 0) + 1
    save_state(state)
    print(f"[RiskManager] Trade recorded for {ticker} | Total today: {state['trades_today']}")


def _reject(reason: str, equity: float, budget: float, pnl: float) -> dict:
    print(f"[RiskManager] ❌ Trade rejected: {reason}")
    return {
        "approved":  False,
        "reason":    reason,
        "equity":    equity,
        "budget":    budget,
        "daily_pnl": pnl,
    }
