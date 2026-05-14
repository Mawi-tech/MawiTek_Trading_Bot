"""
hft_executor.py  —  Strategy 3: Intraday Execution Engine

Fast-cycling execution loop for the HFT intraday scanner.
Scans every 60 seconds (configurable), enters on qualifying setups,
and exits on any of:
    - Take profit: +30% on option premium
    - Stop loss:   -25% on option premium
    - Time stop:   position held > MAX_HOLD_MINUTES
    - EOD flatten: all positions closed 15 minutes before market close

Uses 0-DTE or 1-DTE calls/puts (direction-matched to scanner signal).

Run:
    python hft_executor.py
"""

import time
import datetime
import json
import os

from hft_scanner import run_hft_scan
from tradier_client import (
    get_options_expirations, get_options_chain, get_quote,
    place_option_order, get_open_positions, MOCK_MODE,
)
from risk_manager import pre_trade_check, calculate_contracts, record_trade


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 60        # Re-scan every 60 seconds
SCAN_INTERVAL       = "5m"      # Bar interval for scanner
MIN_SETUP_SCORE     = 55        # Higher bar than overnight catalyst bot
MAX_TRADES_PER_SCAN = 2         # Max new intraday positions per scan
MAX_HOLD_MINUTES    = 45        # Time-stop: close after this many minutes
TAKE_PROFIT_PCT     = 0.30      # Exit at +30% on option premium
STOP_LOSS_PCT       = 0.25      # Exit at -25% on option premium

# DTE selection: 0-DTE first, fall back to 1-DTE
PREFERRED_DTE_MAX   = 1
PREFERRED_DTE_MIN   = 0

# Order settings
USE_LIMIT           = True
LIMIT_BUFFER        = 0.05      # Pay up to 5% above mid

# Market hours (EST)
MARKET_OPEN_H       = 9
MARKET_OPEN_M       = 35
CLOSE_SCAN_H        = 15        # Stop opening new trades at this hour
CLOSE_SCAN_M        = 0
EOD_FLATTEN_H       = 15
EOD_FLATTEN_M       = 15        # Force-close all positions at this time

# State file for intraday position tracking
HFT_STATE_FILE      = "hft_positions.json"


# ─── Position State ────────────────────────────────────────────────────────────

def _load_positions() -> list[dict]:
    if not os.path.exists(HFT_STATE_FILE):
        return []
    try:
        with open(HFT_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_positions(positions: list[dict]):
    with open(HFT_STATE_FILE, "w") as f:
        json.dump(positions, f, indent=2, default=str)


def _add_position(position: dict):
    positions = _load_positions()
    positions.append(position)
    _save_positions(positions)


def _remove_position(option_symbol: str):
    positions = _load_positions()
    positions = [p for p in positions if p.get("option_symbol") != option_symbol]
    _save_positions(positions)


# ─── Option Selection (0–1 DTE) ────────────────────────────────────────────────

def _spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else 1.0


def select_intraday_option(
    ticker: str,
    direction: str,
    budget: float,
) -> dict | None:
    """
    Pick the best 0-DTE or 1-DTE contract matching the signal direction.

    direction: "bullish" → call, "bearish" → put.
    Returns best contract dict with _mid_price injected, or None.
    """
    exps = get_options_expirations(ticker)
    if not exps:
        return None

    stock_price = get_quote(ticker)
    if stock_price <= 0:
        return None

    today = datetime.date.today()
    valid_exps = []
    for exp in exps:
        exp_date = datetime.date.fromisoformat(exp)
        dte = (exp_date - today).days
        if PREFERRED_DTE_MIN <= dte <= PREFERRED_DTE_MAX:
            valid_exps.append((dte, exp))

    if not valid_exps:
        # Widen to 2 DTE
        for exp in exps:
            exp_date = datetime.date.fromisoformat(exp)
            dte = (exp_date - today).days
            if dte == 2:
                valid_exps.append((dte, exp))

    if not valid_exps:
        print(f"[HFT Exec] No 0–2 DTE expirations for {ticker}")
        return None

    # Prefer nearest DTE
    valid_exps.sort(key=lambda x: x[0])
    option_type = "call" if direction == "bullish" else "put"

    for dte, exp in valid_exps:
        chain = get_options_chain(ticker, exp)
        if not chain:
            continue

        legs = [
            c for c in chain
            if c.get("option_type") == option_type
            and float(c.get("bid", 0) or 0) > 0
            and float(c.get("ask", 0) or 0) > 0
            and int(c.get("open_interest", 0) or 0) >= 10
        ]
        if not legs:
            continue

        # Prefer slightly OTM for more leverage
        if direction == "bullish":
            target = stock_price * 1.005
        else:
            target = stock_price * 0.995

        legs.sort(key=lambda c: abs(float(c.get("strike", 0)) - target))

        for contract in legs[:5]:
            bid = float(contract.get("bid", 0))
            ask = float(contract.get("ask", 0))
            if _spread_pct(bid, ask) > 0.30:  # Allow up to 30% spread for 0-DTE
                continue
            mid = round((bid + ask) / 2, 2)
            contract["_mid_price"]   = mid
            contract["_expiration"]  = exp
            contract["_dte"]         = dte
            contract["_option_type"] = option_type
            return contract

    print(f"[HFT Exec] No qualifying {option_type} contract for {ticker}")
    return None


# ─── Position Monitor ──────────────────────────────────────────────────────────

def monitor_hft_positions():
    """
    Check each tracked HFT position against exit rules:
    - Take profit (+30%)
    - Stop loss (-25%)
    - Time stop (> MAX_HOLD_MINUTES)
    """
    positions = _load_positions()
    if not positions:
        return

    now = datetime.datetime.now()
    to_close = []

    for pos in positions:
        option_symbol = pos["option_symbol"]
        entry_price   = pos["entry_price"]
        entry_time    = datetime.datetime.fromisoformat(pos["entry_time"])
        quantity      = pos["quantity"]

        # Time stop
        hold_minutes = (now - entry_time).total_seconds() / 60
        if hold_minutes >= MAX_HOLD_MINUTES:
            print(f"[HFT Exec] ⏱ Time stop hit for {option_symbol} ({hold_minutes:.0f}m held)")
            to_close.append((pos, "time_stop"))
            continue

        # Get current price from chain (use chain lookup)
        # In MOCK_MODE we can't get live price — skip P&L check
        if MOCK_MODE:
            continue

        # Pull current mid from the chain
        underlying = pos.get("underlying", "")
        expiration = pos.get("expiration", "")
        if not underlying or not expiration:
            continue

        chain = get_options_chain(underlying, expiration)
        current_contract = next(
            (c for c in chain if c.get("symbol") == option_symbol), None
        )
        if not current_contract:
            continue

        bid = float(current_contract.get("bid", 0) or 0)
        ask = float(current_contract.get("ask", 0) or 0)
        if bid <= 0:
            continue
        current_mid = (bid + ask) / 2

        pct_change = (current_mid - entry_price) / entry_price

        if pct_change >= TAKE_PROFIT_PCT:
            print(
                f"[HFT Exec] 🎯 TP hit {option_symbol} | "
                f"Entry ${entry_price:.2f} → Now ${current_mid:.2f} "
                f"(+{pct_change:.1%})"
            )
            to_close.append((pos, "take_profit"))

        elif pct_change <= -STOP_LOSS_PCT:
            print(
                f"[HFT Exec] 🛑 SL hit {option_symbol} | "
                f"Entry ${entry_price:.2f} → Now ${current_mid:.2f} "
                f"({pct_change:.1%})"
            )
            to_close.append((pos, "stop_loss"))

    for pos, reason in to_close:
        _close_position(pos, reason)


def _close_position(pos: dict, reason: str):
    """Place a sell_to_close order for a tracked HFT position."""
    option_symbol = pos["option_symbol"]
    underlying    = pos.get("underlying", pos["option_symbol"])
    quantity      = pos["quantity"]

    print(f"[HFT Exec] Closing {option_symbol} x{quantity} — reason: {reason}")

    order = place_option_order(
        symbol=underlying,
        option_symbol=option_symbol,
        side="sell_to_close",
        quantity=quantity,
        order_type="market",
    )

    if order.get("status") != "error":
        _remove_position(option_symbol)
        print(f"[HFT Exec] ✅ Closed {option_symbol}")
    else:
        print(f"[HFT Exec] ❌ Close failed for {option_symbol}: {order.get('error')}")


def flatten_all_positions():
    """Force-close all HFT positions (called at EOD)."""
    positions = _load_positions()
    if not positions:
        return
    print(f"[HFT Exec] EOD flatten — closing {len(positions)} position(s)")
    for pos in positions:
        _close_position(pos, "eod_flatten")


# ─── Trade Entry ───────────────────────────────────────────────────────────────

def execute_hft_trade(setup: dict) -> bool:
    """
    Open one intraday options position for a qualifying HFT setup.
    Returns True if the order was placed.
    """
    ticker    = setup["ticker"]
    direction = setup.get("direction", "bullish")
    score     = setup.get("setup_score", 0)

    if direction not in ("bullish", "bearish"):
        print(f"[HFT Exec] {ticker} — ambiguous direction '{direction}', skipping")
        return False

    print(f"\n[HFT Exec] --- {ticker} | {direction.upper()} | Score: {score} ---")

    risk = pre_trade_check(ticker)
    if not risk["approved"]:
        print(f"[HFT Exec] {ticker} blocked: {risk['reason']}")
        return False

    # Use a smaller position size for intraday (1% per trade vs 2%)
    budget = risk["equity"] * 0.01

    contract = select_intraday_option(ticker, direction, budget)
    if not contract:
        return False

    mid_price     = contract["_mid_price"]
    option_symbol = contract.get("symbol", "")
    expiration    = contract["_expiration"]
    dte           = contract["_dte"]
    strike        = float(contract.get("strike", 0))
    opt_type      = contract["_option_type"]

    quantity = calculate_contracts(budget, mid_price)
    if quantity <= 0:
        print(
            f"[HFT Exec] Budget ${budget:.0f} < cost of 1 contract "
            f"(${mid_price * 100:.0f})"
        )
        return False

    limit_price = round(mid_price * (1 + LIMIT_BUFFER), 2) if USE_LIMIT else None
    order_type  = "limit" if USE_LIMIT else "market"

    print(
        f"[HFT Exec] Placing {order_type} | {ticker} ${strike:.0f}{opt_type[0].upper()} "
        f"{expiration} ({dte}DTE) | x{quantity} @ ${limit_price or 'mkt'}"
    )

    order = place_option_order(
        symbol=ticker,
        option_symbol=option_symbol,
        side="buy_to_open",
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
    )

    if order.get("status") == "error":
        print(f"[HFT Exec] ❌ Order failed: {order.get('error')}")
        return False

    # Track the position
    _add_position({
        "option_symbol": option_symbol,
        "underlying":    ticker,
        "expiration":    expiration,
        "strike":        strike,
        "option_type":   opt_type,
        "direction":     direction,
        "entry_price":   mid_price,
        "quantity":      quantity,
        "entry_time":    datetime.datetime.now().isoformat(),
        "setup_score":   score,
        "dte":           dte,
    })

    record_trade(ticker)
    print(
        f"[HFT Exec] ✅ Trade open | {ticker} ${strike:.0f}{opt_type[0].upper()} | "
        f"x{quantity} @ ${mid_price:.2f} | DTE: {dte}"
    )
    return True


# ─── Market Hours Helpers ──────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0)
    close_t = now.replace(hour=EOD_FLATTEN_H,  minute=EOD_FLATTEN_M,  second=0)
    return open_t <= now <= close_t


def _is_new_trade_allowed() -> bool:
    """No new entries in the last 30 minutes of the session."""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    open_t    = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0)
    cutoff_t  = now.replace(hour=CLOSE_SCAN_H,   minute=CLOSE_SCAN_M,   second=0)
    return open_t <= now <= cutoff_t


def _is_eod_flatten_time() -> bool:
    now = datetime.datetime.now()
    flatten_t = now.replace(hour=EOD_FLATTEN_H, minute=EOD_FLATTEN_M, second=0)
    close_t   = now.replace(hour=15, minute=45, second=0)
    return flatten_t <= now <= close_t


# ─── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    print("\n" + "=" * 60)
    print("  HFT INTRADAY BOT  —  Strategy 3  —  STARTING")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if MOCK_MODE:
        print("  ⚠  MOCK_MODE — no real orders will be placed")
    print(f"  TP: +{TAKE_PROFIT_PCT:.0%}  |  SL: -{STOP_LOSS_PCT:.0%}  |  "
          f"Time stop: {MAX_HOLD_MINUTES}m")
    print("=" * 60)

    while True:
        try:
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if not _is_market_open():
                print(f"[HFT Exec] [{now_str}] Market closed — sleeping 60s")
                time.sleep(60)
                continue

            # EOD: force flatten then sleep until next session
            if _is_eod_flatten_time():
                flatten_all_positions()
                print(f"[HFT Exec] [{now_str}] EOD flatten done — sleeping 30m")
                time.sleep(1800)
                continue

            print(f"\n[HFT Exec] [{now_str}] ── cycle start ──")

            # Step 1: monitor and exit open positions
            monitor_hft_positions()

            # Step 2: open new trades only if time allows
            if _is_new_trade_allowed():
                setups = run_hft_scan(
                    interval=SCAN_INTERVAL,
                    min_score=MIN_SETUP_SCORE,
                    universe_limit=75,
                )

                trades_placed = 0
                for setup in setups:
                    if trades_placed >= MAX_TRADES_PER_SCAN:
                        break
                    if execute_hft_trade(setup):
                        trades_placed += 1

                print(
                    f"[HFT Exec] [{now_str}] Cycle done | "
                    f"New trades: {trades_placed} | "
                    f"Open positions: {len(_load_positions())}"
                )
            else:
                print(f"[HFT Exec] [{now_str}] Past new-trade cutoff — monitoring only")

        except KeyboardInterrupt:
            print("\n[HFT Exec] Stopped by user.")
            break
        except Exception as e:
            print(f"[HFT Exec] Error: {e} — retrying in 30s")
            time.sleep(30)
            continue

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HFT Intraday Executor — Strategy 3")
    parser.add_argument("--flatten", action="store_true",
                        help="Immediately flatten all open HFT positions and exit")
    parser.add_argument("--show-positions", action="store_true",
                        help="Print current tracked HFT positions and exit")
    args = parser.parse_args()

    if args.flatten:
        flatten_all_positions()
    elif args.show_positions:
        positions = _load_positions()
        if not positions:
            print("No open HFT positions tracked.")
        else:
            print(f"\n{len(positions)} open HFT position(s):\n")
            for p in positions:
                held = (
                    datetime.datetime.now() -
                    datetime.datetime.fromisoformat(p["entry_time"])
                ).total_seconds() / 60
                print(
                    f"  {p['option_symbol']} | {p['direction']} | "
                    f"Entry: ${p['entry_price']:.2f} | "
                    f"x{p['quantity']} | Held: {held:.0f}m"
                )
    else:
        run()
