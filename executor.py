"""
executor.py — Strategy 1: Catalyst Long-Call Execution Engine

Main execution loop for the catalyst (earnings + news + flow + momentum)
strategy. Buys 7–30 DTE long calls when the scanner finds setups above the
score floor. One process per strategy; risk + position state is shared via
state_io with the other strategies.

Flow:
    1. Recover any in-flight orders from a prior crash (order_manager ledger)
    2. Reconcile local positions vs the broker, rebuild halt state
    3. Loop:
         a. run_options_scanner() — rank candidates ANY time (incl. market
            closed), rotating through the full-market universe, so the
            dashboard always shows fresh opportunities
         b. While the market is OPEN: monitor_positions() (manage exits), then
            for each setup ≥ MIN_SETUP_SCORE: pre_trade_check → select_option
            → place_and_confirm → record_entry → notify; snapshot equity
         c. While CLOSED: scan + surface setups only (no orders); post-close,
            fire the daily summary
         d. Write dashboard state
         e. Sleep SCAN_INTERVAL_SECONDS (open) / CLOSED_SCAN_INTERVAL_SECONDS (closed)

Auditing:
    Every decision (traded / rejected / considered-but-below-score / capped)
    is persisted to decision_log.jsonl so the Decision Log tab can answer
    "why did the bot do — or NOT do — what it did?" later. Every position
    entry carries the setup score + signal snapshot so closed-trade records
    can show what the setup looked like at entry months later.
"""

import time
import datetime

from options_scanner import run_options_scanner
from universe import scan_csv
from option_selector import select_option
from risk_manager import pre_trade_check, size_contracts, record_trade, reconcile_from_broker
from position_manager import monitor_positions, record_entry
from order_manager import place_and_confirm, recover_pending_orders
from dashboard_state import write_dashboard_state
from heartbeat import beat
from utils import now_est, today_est
from decision_log import (
    log_decision,
    ACTION_TRADED, ACTION_REJECTED, ACTION_CONSIDERED,
)
from equity_tracker import snapshot_equity


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SECONDS  = 300    # Re-scan every 5 minutes while the market is open
CLOSED_SCAN_INTERVAL_SECONDS = 1800  # After-hours: scan every 30 min (daily data is static)
SCAN_UNIVERSE_LIMIT    = 200    # Names scanned per cycle (rotates through the full market)
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
    now = now_est()
    # Skip weekends
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return open_time <= now <= close_time


def calculate_limit_price(mid: float) -> float:
    """Slightly above mid to improve fill probability."""
    return round(mid * (1 + LIMIT_ORDER_BUFFER), 2)


# ─── Single Trade Execution ────────────────────────────────────────────────────

def execute_trade(setup: dict) -> bool:
    """
    Execute a single options trade for a given setup.

    Returns True if order was placed successfully.

    Every rejection path now also writes a decision_log entry so the
    dashboard can answer "why did the bot NOT trade NVDA?" later.
    """
    ticker          = setup["ticker"]
    earnings_days   = setup.get("days_until_earnings")
    setup_score     = setup.get("setup_score", 0)
    # Capture the signal snapshot for journaling at entry time.
    signal_snapshot = {
        "iv_rank":        setup.get("iv_rank"),
        "momentum_score": setup.get("momentum_score"),
        "news_score":     setup.get("news_score"),
        "flow_score":     setup.get("flow_score"),
        "earnings_days":  earnings_days,
    }

    print(f"\n[Executor] --- Processing {ticker} (Score: {setup_score}/100) ---")

    # Step 1: Risk check (with per-strategy capital cap)
    risk = pre_trade_check(ticker, strategy="catalyst_long_call")
    if not risk["approved"]:
        print(f"[Executor] {ticker} blocked by risk manager: {risk['reason']}")
        log_decision(
            ticker = ticker,
            action = ACTION_REJECTED,
            reason = f"risk: {risk['reason']}",
            score  = setup_score,
            extras = {"daily_pnl": risk.get("daily_pnl")},
        )
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
        log_decision(
            ticker = ticker,
            action = ACTION_REJECTED,
            reason = "no qualifying contract found",
            score  = setup_score,
        )
        return False

    # Step 3: Calculate contracts
    mid_price = contract.get("_mid_price", 0)
    if mid_price <= 0:
        print(f"[Executor] Invalid mid price for {ticker} contract")
        log_decision(
            ticker = ticker,
            action = ACTION_REJECTED,
            reason = "invalid mid price on selected contract",
            score  = setup_score,
        )
        return False

    quantity = size_contracts(budget, mid_price, risk["equity"], strategy="catalyst_long_call", contract=contract)
    if quantity <= 0:
        print(
            f"[Executor] {ticker} — budget ${budget:.0f} not enough for "
            f"1 contract at ${mid_price:.2f} (${mid_price*100:.0f}/contract)"
        )
        log_decision(
            ticker = ticker,
            action = ACTION_REJECTED,
            reason = f"budget ${budget:.0f} < cost ${mid_price*100:.0f} per contract",
            score  = setup_score,
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

    # Step 5: Place order AND confirm it actually filled. place_and_confirm
    # polls the broker until the order reaches a terminal state, so we record
    # the REAL fill price/quantity instead of assuming a mid-price fill.
    # Note: Tradier option orders take the UNDERLYING as `symbol`, the OCC
    # string as `option_symbol`.
    fill = place_and_confirm(
        symbol=ticker,
        option_symbol=option_symbol,
        side="buy_to_open",
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
        strategy="catalyst_long_call",
        fallback_price=mid_price,
    )

    if not fill.ok or fill.filled_qty <= 0:
        print(f"[Executor] ❌ Order did not fill for {ticker}: {fill.reason}")
        log_decision(
            ticker = ticker,
            action = ACTION_REJECTED,
            reason = f"order not filled ({fill.status}): {fill.reason}",
            score  = setup_score,
        )
        return False

    # Use the ACTUAL fill, not the mid we asked for.
    fill_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else mid_price
    filled_qty = int(fill.filled_qty)
    if fill.partially_filled:
        print(f"[Executor] ⚠ Partial fill on {ticker}: {filled_qty}/{quantity} contracts")

    # Step 6: Record position at the real fill price/quantity
    earnings_date_str = None
    if earnings_days is not None:
        # ET-anchored so the cached "days until earnings" we already computed
        # in ET stays consistent when projected back to an absolute date.
        earnings_date = today_est() + datetime.timedelta(days=earnings_days)
        earnings_date_str = earnings_date.isoformat()

    record_entry(
        option_symbol=option_symbol,
        underlying=ticker,
        entry_price=fill_price,
        quantity=filled_qty,
        expiration=expiration,
        earnings_date=earnings_date_str,
        setup_score=setup_score,
        signals=signal_snapshot,
        strategy="catalyst_long_call",
    )

    record_trade(ticker)

    log_decision(
        ticker = ticker,
        action = ACTION_TRADED,
        reason = f"score {setup_score} ≥ {MIN_SETUP_SCORE}, filled {filled_qty} @ ${fill_price:.2f}",
        score  = setup_score,
        extras = {
            "option_symbol": option_symbol,
            "strike":        strike,
            "expiration":    expiration,
            "dte":           dte,
            "quantity":      filled_qty,
            "entry_price":   round(fill_price, 4),
            "cost":          round(filled_qty * fill_price * 100, 2),
            "order_id":      fill.order_id,
            "fill_status":   fill.status,
            "signals":       signal_snapshot,
        },
        force = True,   # never collapse an actual fill out of the audit log
    )

    print(
        f"[Executor] ✅ Trade filled | {ticker} ${strike}C | "
        f"x{filled_qty} @ ${fill_price:.2f} | DTE: {dte} | "
        f"Cost: ~${filled_qty * fill_price * 100:,.0f}"
    )

    # Push a fill notification to any configured channel.
    try:
        from event_notifier import notify_trade_filled
        notify_trade_filled(
            strategy = "catalyst_long_call",
            ticker   = ticker,
            contract = f"${strike}C {expiration} ({dte}DTE)",
            qty      = filled_qty,
            price    = fill_price,
            cost     = round(filled_qty * fill_price * 100, 2),
        )
    except Exception as e:
        print(f"[Executor] notify_trade_filled failed: {e}")

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
    print(f"  {now_est().strftime('%Y-%m-%d %H:%M:%S')} EST")
    print("="*60)

    # Resolve any orders left in-flight by a previous crash BEFORE touching
    # risk state, so a fill that completed while we were down is accounted for.
    try:
        recovered = recover_pending_orders()
        for r in recovered:
            if r.ok and r.filled_qty > 0:
                print(f"[Executor] Recovered fill from prior session: {r.tag} — {r.reason}")
    except Exception as e:
        print(f"[Executor] Pending-order recovery failed (non-fatal): {e}")

    # Sync risk state with the broker before scanning. Prevents the
    # halt flag and realized-P&L number from going stale after a crash
    # or restart mid-session — they get rebuilt from live equity.
    reconcile_from_broker()

    while True:
        market_open = is_market_hours()
        try:
            now = now_est().strftime("%H:%M:%S")

            # The scanner runs EVERY cycle — even when the market is closed — so
            # the dashboard surfaces opportunities to review pre-market, after
            # hours, and on weekends. Only ORDER EXECUTION and exit management
            # (which need live option prices) are gated to market hours below.
            if market_open:
                print(f"\n[Executor] [{now}] Starting scan cycle...\n")
                beat("executor", status="scanning")
            else:
                print(f"\n[Executor] [{now}] Market closed — scanning for setups only (no trades).\n")
                beat("executor", status="scanning_closed")

            # Write scanning status immediately so dashboard shows activity
            write_dashboard_state(
                setups=[],
                bot_status="scanning" if market_open else "scanning_closed",
                account_mode=ACCOUNT_MODE,
            )

            # Step 1: Monitor and manage exits on open positions — only while the
            # market is open (closing on stale/crossed after-hours quotes is risky).
            if market_open:
                monitor_positions()

            # Step 2: Scan for new setups (rotates through the liquid universe;
            # own rotation_key so it sweeps independently of the other scanners)
            setups = run_options_scanner(
                csv_path=scan_csv(),
                universe_limit=SCAN_UNIVERSE_LIMIT,
                output_csv=False,
                rotation_key="catalyst",
            )

            # Step 3: Filter by minimum score
            qualified = [s for s in setups if s.get("setup_score", 0) >= MIN_SETUP_SCORE]
            print(f"\n[Executor] {len(qualified)} setups above score threshold ({MIN_SETUP_SCORE})")

            # Alert subscribers to fresh swing candidates (deduped per day,
            # never blocks trading).
            try:
                from event_notifier import notify_trade_setups
                notify_trade_setups(qualified, style="swing", strategy="catalyst_long_call")
            except Exception as e:
                print(f"[Executor] setup alert failed (non-fatal): {e}")

            # Log every below-threshold setup so the Decision Log shows
            # WHY they weren't traded, not just that they weren't.
            for setup in setups:
                if setup.get("setup_score", 0) < MIN_SETUP_SCORE:
                    log_decision(
                        ticker = setup.get("ticker", "?"),
                        action = ACTION_CONSIDERED,
                        reason = f"score {setup.get('setup_score', 0)} < threshold {MIN_SETUP_SCORE}",
                        score  = setup.get("setup_score", 0),
                    )

            # Step 4: Execute top setups (cap at MAX_TRADES_PER_SCAN) — only when
            # the market is open. When closed we scanned for visibility only.
            trades_placed = 0
            if market_open:
                for setup in qualified[:MAX_TRADES_PER_SCAN]:
                    if trades_placed >= MAX_TRADES_PER_SCAN:
                        break

                    success = execute_trade(setup)
                    if success:
                        trades_placed += 1

                # Also log qualified setups we didn't execute because the cap fired,
                # so "we had 5 good setups but only traded 2" is auditable.
                for setup in qualified[MAX_TRADES_PER_SCAN:]:
                    log_decision(
                        ticker = setup.get("ticker", "?"),
                        action = ACTION_CONSIDERED,
                        reason = f"qualified but capped by MAX_TRADES_PER_SCAN ({MAX_TRADES_PER_SCAN})",
                        score  = setup.get("setup_score", 0),
                    )
            elif qualified:
                print(f"[Executor] {len(qualified)} qualifying setup(s) found but market "
                      f"is closed — surfaced on the dashboard, not traded.")

            # Step 5: Write full dashboard state after cycle completes
            write_dashboard_state(
                setups=setups,
                bot_status="running" if market_open else "scanning_closed",
                account_mode=ACCOUNT_MODE,
            )

            # Step 6: True mark-to-market equity snapshot — only while the market
            # is open (after-hours marks are stale). Done after the dashboard
            # write so any errors here don't blank the UI.
            if market_open:
                try:
                    snap = snapshot_equity()
                    if snap:
                        print(
                            f"[Executor] Equity snapshot | "
                            f"Equity: ${snap['equity']:,.2f} | "
                            f"Unrealized: ${snap['unrealized_pnl']:+,.2f} | "
                            f"Realized today: ${snap['realized_today']:+,.2f}"
                        )
                except Exception as e:
                    print(f"[Executor] Equity snapshot failed (non-fatal): {e}")

            # Step 7: Send the end-of-day digest once per day, AFTER the close
            # (weekdays only). Runs on the closed-market scan cycles.
            if not market_open:
                _n = now_est()
                _past_close = (_n.hour, _n.minute) >= (MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN)
                if _n.weekday() < 5 and _past_close:
                    try:
                        from daily_report import maybe_send_eod_summary
                        maybe_send_eod_summary()
                    except Exception as e:
                        print(f"[Executor] daily summary failed (non-fatal): {e}")

            _sleep_s = SCAN_INTERVAL_SECONDS if market_open else CLOSED_SCAN_INTERVAL_SECONDS
            print(
                f"\n[Executor] Cycle complete | "
                f"Trades placed: {trades_placed} | "
                f"Sleeping {_sleep_s}s...\n"
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

        time.sleep(SCAN_INTERVAL_SECONDS if market_open else CLOSED_SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
