"""
executor.py

Main execution engine for the options catalyst bot.

Flow:
1. Run options scanner → get ranked setups
2. For each setup, run pre-trade risk check
3. Select best option contract (expiry + strike)
4. Calculate contracts based on risk sizing
5. Place buy_to_open order via Tradier
6. Record position for monitoring
7. Monitor open positions for exits

Run this as your main bot loop — it handles both
entry scanning and position management in one loop.
"""

import time
import datetime

from options_scanner import run_options_scanner
from option_selector import select_option
from risk_manager import pre_trade_check, calculate_contracts, record_trade
from position_manager import monitor_positions, record_entry
from tradier_client import place_option_order
from dashboard_state import write_dashboard_state


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SECONDS  = 300    # Re-scan every 5 minutes
MIN_SETUP_SCORE        = 50     # Only trade setups scoring 50+/100
MAX_TRADES_PER_SCAN    = 2      # Max new positions per scan cycle
USE_LIMIT_ORDERS       = True   # Limit orders for better fills
LIMIT_ORDER_BUFFER     = 0.05   # Pay up to 5% above mid for fills
ACCOUNT_MODE           = "paper"  # Change to "live" when ready

# Market hours guard (EST)
MARKET_OPEN_HOUR  = 9
MARKET_OPEN_MIN   = 35   # Start 5 min after open (avoid opening volatility)
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN  = 30   # Stop 30 min before close


# ─── Helpers ───────────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.datetime.now()
    # Skip weekends
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return open_time <= now <= close_time


def calculate_limit_price(mid: float) -> float:
    """Slightly above mid to improve fill probability."""
    return round(mid * (1 + LIMIT_ORDER_BUFFER), 2)


# ─── Single Trade Execution ────────────────────────────────────────────────────

def execute_trade(setup: dict) -> bool:
    """
    Execute a single options trade for a given setup.

    Returns True if order was placed successfully.
    """
    ticker          = setup["ticker"]
    earnings_days   = setup.get("days_until_earnings")
    setup_score     = setup.get("setup_score", 0)

    print(f"\n[Executor] --- Processing {ticker} (Score: {setup_score}/100) ---")

    # Step 1: Risk check
    risk = pre_trade_check(ticker)
    if not risk["approved"]:
        print(f"[Executor] {ticker} blocked by risk manager: {risk['reason']}")
        return False

    budget = risk["budget"]

    # Step 2: Select best option
    contract = select_option(
        ticker=ticker,
        days_until_earnings=earnings_days,
        budget_per_contract=budget,
    )

    if not contract:
        print(f"[Executor] No qualifying contract found for {ticker}")
        return False

    # Step 3: Calculate contracts
    mid_price = contract.get("_mid_price", 0)
    if mid_price <= 0:
        print(f"[Executor] Invalid mid price for {ticker} contract")
        return False

    quantity = calculate_contracts(budget, mid_price)
    if quantity <= 0:
        print(
            f"[Executor] {ticker} — budget ${budget:.0f} not enough for "
            f"1 contract at ${mid_price:.2f} (${mid_price*100:.0f}/contract)"
        )
        return False

    option_symbol = contract.get("symbol", "")
    expiration    = contract.get("_expiration", "")
    strike        = contract.get("strike", "")
    dte           = contract.get("_dte", 0)

    # Step 4: Determine order type and price
    if USE_LIMIT_ORDERS:
        order_type   = "limit"
        limit_price  = calculate_limit_price(mid_price)
    else:
        order_type  = "market"
        limit_price = None

    print(
        f"[Executor] Placing order | {ticker} ${strike}C {expiration} | "
        f"x{quantity} contracts | {order_type} @ ${limit_price or 'market'} | "
        f"Total: ~${quantity * mid_price * 100:,.0f}"
    )

    # Step 5: Place order
    order = place_option_order(
        symbol=option_symbol,
        option_symbol=option_symbol,
        side="buy_to_open",
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
    )

    if order.get("status") == "error":
        print(f"[Executor] ❌ Order failed for {ticker}: {order.get('error')}")
        return False

    # Step 6: Record position
    earnings_date_str = None
    if earnings_days is not None:
        earnings_date = datetime.date.today() + datetime.timedelta(days=earnings_days)
        earnings_date_str = earnings_date.isoformat()

    record_entry(
        option_symbol=option_symbol,
        underlying=ticker,
        entry_price=mid_price,
        quantity=quantity,
        expiration=expiration,
        earnings_date=earnings_date_str,
    )

    record_trade(ticker)

    print(
        f"[Executor] ✅ Trade placed | {ticker} ${strike}C | "
        f"x{quantity} @ ${mid_price:.2f} | DTE: {dte} | "
        f"Cost: ~${quantity * mid_price * 100:,.0f}"
    )

    return True


# ─── Main Bot Loop ─────────────────────────────────────────────────────────────

def run():
    """
    Main loop:
    - Monitor existing positions every cycle
    - Scan for new setups every cycle
    - Execute qualifying trades (up to MAX_TRADES_PER_SCAN)
    - Sleep and repeat
    """
    print("\n" + "="*60)
    print("  OPTIONS CATALYST BOT — STARTING")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    while True:
        try:
            now = datetime.datetime.now().strftime("%H:%M:%S")

            if not is_market_hours():
                print(f"\n[Executor] [{now}] Market closed — waiting...")
                write_dashboard_state(setups=[], bot_status="idle", account_mode=ACCOUNT_MODE)
                time.sleep(60)
                continue

            print(f"\n[Executor] [{now}] Starting scan cycle...\n")

            # Write scanning status immediately so dashboard shows activity
            write_dashboard_state(setups=[], bot_status="scanning", account_mode=ACCOUNT_MODE)

            # Step 1: Monitor and manage exits on open positions
            monitor_positions()

            # Step 2: Scan for new setups
            setups = run_options_scanner(output_csv=False)

            # Step 3: Filter by minimum score
            qualified = [s for s in setups if s.get("setup_score", 0) >= MIN_SETUP_SCORE]
            print(f"\n[Executor] {len(qualified)} setups above score threshold ({MIN_SETUP_SCORE})")

            # Step 4: Execute top setups (cap at MAX_TRADES_PER_SCAN)
            trades_placed = 0
            for setup in qualified[:MAX_TRADES_PER_SCAN]:
                if trades_placed >= MAX_TRADES_PER_SCAN:
                    break

                success = execute_trade(setup)
                if success:
                    trades_placed += 1

            # Step 5: Write full dashboard state after cycle completes
            write_dashboard_state(
                setups=setups,
                bot_status="running",
                account_mode=ACCOUNT_MODE,
            )

            print(
                f"\n[Executor] Cycle complete | "
                f"Trades placed: {trades_placed} | "
                f"Sleeping {SCAN_INTERVAL_SECONDS}s...\n"
            )

        except KeyboardInterrupt:
            print("\n[Executor] Bot stopped by user.")
            write_dashboard_state(setups=[], bot_status="idle", account_mode=ACCOUNT_MODE)
            break
        except Exception as e:
            print(f"\n[Executor] Unexpected error: {e}")
            write_dashboard_state(setups=[], bot_status="error", account_mode=ACCOUNT_MODE)
            print("[Executor] Continuing after 60s...")
            time.sleep(60)
            continue

        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
