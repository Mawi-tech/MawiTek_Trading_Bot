"""
pead_executor.py  —  Strategy 4: Post-Earnings / News-Drift Executor

Live execution loop for the PEAD / news-drift scanner. Trades the continuation
of a large news-driven gap with directional options (calls on up-drift, puts on
down-drift), held as a multi-day SWING (not flattened intraday).

Exits (checked every cycle):
    - Take profit: +80% on the option premium (let the drift run)
    - Stop loss:   -35% on the option premium
    - Drift fade:  underlying retraces through the pre-event price (gap fill)
    - DTE floor:   close when <= MIN_DTE_EXIT days to expiry
    - Max hold:    close after MAX_HOLD_DAYS (the drift edge decays)

Strategy tag: "pead" (a SWING strategy — see risk_manager.SWING_STRATEGIES).

Run:
    python pead_executor.py
    python pead_executor.py --show-positions
"""

import argparse
import datetime
import time

from mawitek.strategies.pead_scanner import run_pead_scan, MIN_SETUP_SCORE
from mawitek.data.universe import scan_csv
import mawitek.core.exit_manager as exit_manager
import mawitek.core.position_book as _pb
from mawitek.data.tradier_client import (
    get_options_expirations, get_options_chain, get_quote,
    get_open_positions, get_option_mid, MOCK_MODE,
)
from mawitek.core.order_manager import place_and_confirm, recover_pending_orders
from mawitek.core.risk_manager import pre_trade_check, size_contracts, record_trade, reconcile_from_broker
from mawitek.core.position_manager import days_until_expiry
from mawitek.core.trade_journal import record_closed_trade
from mawitek.core.decision_log import log_decision, ACTION_TRADED, ACTION_REJECTED, ACTION_EXITED
from mawitek.infra.logger import get_logger, log_trade
from mawitek.infra.heartbeat import beat
from mawitek.infra.utils import now_est, today_est, parse_isodt, spread_pct as _spread_pct, is_market_open

log = get_logger("pead_executor")


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 1800      # Re-scan every 30 min (daily-bar signal moves slowly)
CLOSED_SCAN_INTERVAL_SEC = 1800 # Same cadence when closed — daily signal is static
SCAN_UNIVERSE_LIMIT = 300       # Names scanned per cycle (rotates through the full market)
MIN_SCAN_SCORE      = MIN_SETUP_SCORE
MAX_TRADES_PER_SCAN = 2         # New swing positions opened per scan

# Exit rules — validated in backtest_pead.py (TP +80% / SL -35% / ~12 trading-day
# hold). Drift is convex on the option, so let winners run and cut losers.
TAKE_PROFIT_PCT     = 0.80
STOP_LOSS_PCT       = 0.35
MAX_HOLD_DAYS       = 16        # calendar days (~12 trading days in the backtest)
MIN_DTE_EXIT        = 3         # close when this few days to expiry remain

# Option selection: 14–35 DTE, target ~21 (matches the backtest's DTE_ENTRY).
PREFERRED_DTE_MIN   = 14
PREFERRED_DTE_MAX   = 35
PREFERRED_DTE_TARGET = 21
MIN_OPEN_INTEREST   = 50
MAX_SPREAD_PCT      = 0.18

# Conviction sizing: a clear earnings/major-news gap ("high") gets full size; a
# moderate gap ("relaxed") is sized down because its edge is weaker.
SIZE_FRAC_HIGH      = 1.0
SIZE_FRAC_RELAXED   = 0.5

# Order settings
USE_LIMIT           = True
LIMIT_BUFFER        = 0.05

# Market hours (ET)
MARKET_OPEN_H, MARKET_OPEN_M = 9, 35
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 55

PEAD_STATE_FILE     = "pead_positions.json"


# ─── Position State ────────────────────────────────────────────────────────────

# Single-leg book logic is shared across the 3 day/swing executors
# (see position_book.py); these wrappers bind it to this strategy's own file.
def _load_positions() -> list[dict]:
    return _pb.load(PEAD_STATE_FILE)


def _save_positions(positions: list[dict]) -> None:
    _pb.save(PEAD_STATE_FILE, positions)


def _add_position(position: dict) -> None:
    _pb.add(PEAD_STATE_FILE, position)


def _remove_position(option_symbol: str) -> None:
    _pb.remove(PEAD_STATE_FILE, option_symbol)


def _update_position(option_symbol: str, **fields) -> None:
    _pb.update(PEAD_STATE_FILE, option_symbol, **fields)


def reconcile_pead_positions() -> int:
    """Drop locally-tracked positions that are no longer open at the broker."""
    local = _load_positions()
    if not local or MOCK_MODE:
        return 0
    try:
        # strict=True → a failed broker read raises instead of returning [],
        # so a transient outage can't make us journal every open position as
        # closed_externally and orphan it from exit management.
        broker_syms = {p.get("symbol") for p in get_open_positions(strict=True) if p.get("symbol")}
    except Exception as e:
        log.warning("reconcile_pead_positions: could not query broker: %s", e)
        return 0

    stale = [p for p in local if p.get("option_symbol") not in broker_syms]
    for pos in stale:
        sym = pos.get("option_symbol", "")
        log.info("Stale PEAD position %s not at broker — journaling closed_externally", sym)
        entry_price = float(pos.get("entry_price", 0) or 0)
        try:
            record_closed_trade(
                option_symbol=sym, underlying=pos.get("underlying", ""),
                entry_price=entry_price, exit_price=entry_price,
                quantity=int(pos.get("quantity", 0) or 0),
                expiration=pos.get("expiration", ""), entry_time=pos.get("entry_time"),
                exit_reason="closed_externally", setup_score=pos.get("setup_score"),
                signals={"direction": pos.get("direction"), "conviction": pos.get("conviction")},
                strategy="pead",
            )
        except Exception as e:
            log.error("Failed to journal stale PEAD position %s: %s", sym, e)
        _remove_position(sym)
    return len(stale)


# ─── Option Selection (directional, 14–35 DTE) ─────────────────────────────────

def select_drift_option(ticker: str, direction: str, budget: float) -> dict | None:
    """
    Pick a liquid, roughly-ATM option in the drift direction.

    direction: "bullish" -> call, "bearish" -> put.
    Returns the chosen contract dict (with _mid_price/_expiration/_dte/etc.)
    or None if nothing qualifies.
    """
    option_type = "call" if direction == "bullish" else "put"

    exps = get_options_expirations(ticker)
    if not exps:
        return None
    spot = get_quote(ticker)
    if spot <= 0:
        return None

    today = today_est()    # ET — anchor DTE math to the market's day, not the host's
    valid = []
    for exp in exps:
        try:
            dte = (datetime.date.fromisoformat(exp) - today).days
        except ValueError:
            continue
        if PREFERRED_DTE_MIN <= dte <= PREFERRED_DTE_MAX:
            valid.append((dte, exp))
    if not valid:
        return None

    # Expiry nearest the target DTE.
    valid.sort(key=lambda x: abs(x[0] - PREFERRED_DTE_TARGET))

    for dte, exp in valid:
        chain = get_options_chain(ticker, exp)
        legs = [
            c for c in chain
            if c.get("option_type") == option_type
            and float(c.get("bid", 0) or 0) > 0
            and float(c.get("ask", 0) or 0) > 0
            and int(c.get("open_interest", 0) or 0) >= MIN_OPEN_INTEREST
        ]
        if not legs:
            continue

        # Closest to ATM (strike nearest spot).
        legs.sort(key=lambda c: abs(float(c.get("strike", 0)) - spot))
        for contract in legs[:6]:
            bid = float(contract.get("bid", 0))
            ask = float(contract.get("ask", 0))
            if _spread_pct(bid, ask) > MAX_SPREAD_PCT:
                continue
            mid = round((bid + ask) / 2, 2)
            if mid <= 0 or mid * 100 > budget:
                continue
            contract["_mid_price"] = mid
            contract["_expiration"] = exp
            contract["_dte"] = dte
            contract["_option_type"] = option_type
            return contract

    log.warning("No qualifying %s contract for %s", option_type, ticker)
    return None


# ─── Position Monitor ──────────────────────────────────────────────────────────

def monitor_pead_positions():
    """Check each open position against the exit rules and close as needed."""
    positions = _load_positions()
    if not positions:
        return

    to_close: list[tuple[dict, str]] = []
    to_scale: list[tuple[dict, int]] = []
    peaks_dirty = False
    for pos in positions:
        option_symbol = pos["option_symbol"]
        entry_price = float(pos.get("entry_price", 0) or 0)
        if entry_price <= 0:
            continue

        # DTE floor
        dte = days_until_expiry(pos.get("expiration", ""))
        if dte <= MIN_DTE_EXIT:
            to_close.append((pos, "dte_exit"))
            continue

        # Max hold — tz-aware via parse_isodt so legacy naive entry_time records
        # still subtract cleanly against now_est().
        try:
            entry_dt = parse_isodt(pos["entry_time"])
            if (now_est() - entry_dt).days >= MAX_HOLD_DAYS:
                to_close.append((pos, "time_stop"))
                continue
        except Exception:
            pass

        if MOCK_MODE:
            continue

        # Drift fade: underlying retraced through the pre-event price (gap fill).
        prev_close = float(pos.get("prev_close", 0) or 0)
        if prev_close > 0:
            spot = get_quote(pos.get("underlying", ""))
            if spot > 0:
                if pos.get("direction") == "bullish" and spot <= prev_close:
                    to_close.append((pos, "drift_fade"))
                    continue
                if pos.get("direction") == "bearish" and spot >= prev_close:
                    to_close.append((pos, "drift_fade"))
                    continue

        # TP / SL on the option premium
        mid = get_option_mid(option_symbol, pos.get("underlying", ""), pos.get("expiration", ""))
        if mid <= 0:
            continue
        pnl_pct = (mid - entry_price) / entry_price

        # Trailing stop + scale-out on top of the fixed TP/SL.
        peak = exit_manager.update_peak(pos, pnl_pct)
        peaks_dirty = True
        scale_qty = exit_manager.scale_out_quantity(pos, pnl_pct, exit_manager.PEAD_EXIT)
        if scale_qty > 0:
            to_scale.append((pos, scale_qty))
            continue
        if exit_manager.trailing_stop_hit(pnl_pct, peak, exit_manager.PEAD_EXIT):
            to_close.append((pos, "trailing_stop"))
            continue
        if pnl_pct >= TAKE_PROFIT_PCT:
            to_close.append((pos, "take_profit"))
        elif pnl_pct <= -STOP_LOSS_PCT:
            to_close.append((pos, "stop_loss"))

    if peaks_dirty:
        _save_positions(positions)
    for pos, qty in to_scale:
        _close_position(pos, "scale_out", close_qty=qty)
    for pos, reason in to_close:
        _close_position(pos, reason)


def _close_position(pos: dict, reason: str, close_qty: int | None = None):
    """Sell-to-close a tracked position — fully, or just `close_qty` contracts for a
    partial scale-out (the remainder stays open as a runner). Journals the slice."""
    option_symbol = pos["option_symbol"]
    underlying = pos.get("underlying", "")
    entry_price = float(pos.get("entry_price", 0) or 0)
    quantity = int(pos.get("quantity", 0) or 0)
    qty_to_close = quantity if close_qty is None else max(1, min(int(close_qty), quantity))
    partial = qty_to_close < quantity

    log.info("Closing %s x%d%s — reason: %s", option_symbol, qty_to_close,
             " (scale-out)" if partial else "", reason)
    fill = place_and_confirm(
        symbol=underlying, option_symbol=option_symbol, side="sell_to_close",
        quantity=qty_to_close, order_type="market", strategy="pead",
        fallback_price=entry_price, timeout=30.0,
    )

    if not (fill.ok and fill.filled_qty > 0):
        log.error("Close did NOT fill for %s: %s — will retry next cycle",
                  option_symbol, fill.reason)
        return

    closed_qty = int(fill.filled_qty)
    remaining  = quantity - closed_qty
    if remaining > 0:
        _update_position(option_symbol, quantity=remaining, scaled_out=True)
    else:
        _remove_position(option_symbol)
    exit_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else entry_price
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    pnl_dollar = round((exit_price - entry_price) * closed_qty * 100, 2)
    quantity = closed_qty   # journal/log the closed slice below
    log.info("Closed %s x%d | %s | P&L %+.1f%% ($%+.2f)%s", option_symbol, closed_qty,
             reason, pnl_pct, pnl_dollar, f" | {remaining} left running" if remaining > 0 else "")

    log_decision(
        ticker=underlying, action=ACTION_EXITED, strategy="pead",
        reason=reason,
        extras={"option_symbol": option_symbol,
                "entry_price": entry_price, "exit_price": round(exit_price, 4),
                "pnl_pct": round(pnl_pct, 2), "pnl_dollar": pnl_dollar},
        force=True,
    )

    try:
        record_closed_trade(
            option_symbol=option_symbol, underlying=underlying, entry_price=entry_price,
            exit_price=exit_price, quantity=quantity, expiration=pos.get("expiration", ""),
            entry_time=pos.get("entry_time"), exit_reason=reason,
            setup_score=pos.get("setup_score"),
            signals={"direction": pos.get("direction"), "conviction": pos.get("conviction"),
                     "event_move": pos.get("event_move")},
            strategy="pead",
        )
    except Exception as e:
        log.error("Failed to journal PEAD close for %s: %s", option_symbol, e)

    try:
        from mawitek.infra.event_notifier import notify_position_closed
        opt = (pos.get("option_type", "") or "?")[0].upper()
        notify_position_closed(
            ticker=underlying, contract=f"${pos.get('strike', 0):.0f}{opt}",
            pnl_dollar=pnl_dollar, pnl_pct=pnl_pct, reason=reason, strategy="pead",
        )
    except Exception as e:
        log.warning("notify_position_closed failed: %s", e)


# ─── Trade Entry ───────────────────────────────────────────────────────────────

def execute_pead_trade(setup: dict) -> bool:
    """Open one drift position for a qualifying setup. Returns True if placed."""
    ticker = setup["ticker"]
    direction = setup.get("direction", "bullish")
    score = setup.get("setup_score", 0)
    conviction = setup.get("conviction", "relaxed")

    if direction not in ("bullish", "bearish"):
        return False

    # Don't stack a second position on a name we already hold.
    if any(p.get("underlying") == ticker for p in _load_positions()):
        log.info("Already hold a PEAD position on %s — skipping", ticker)
        return False

    log.info("Processing %s | %s | score %d | %s", ticker, direction.upper(), score, conviction)

    risk = pre_trade_check(ticker, strategy="pead")
    if not risk["approved"]:
        log.warning("Blocked %s — %s", ticker, risk["reason"])
        log_trade({"strategy": "pead", "ticker": ticker, "approved": False,
                   "reason": risk["reason"], "setup_score": score, "direction": direction})
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="pead",
            reason=f"risk: {risk['reason']}", score=score,
            extras={"direction": direction, "conviction": conviction,
                    "event_move": setup.get("event_move"),
                    "style_reason": setup.get("style_reason")},
        )
        return False

    # Size by conviction.
    size_frac = SIZE_FRAC_HIGH if conviction == "high" else SIZE_FRAC_RELAXED
    budget = risk["budget"] * size_frac

    contract = select_drift_option(ticker, direction, budget)
    if not contract:
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="pead",
            reason=f"no qualifying {PREFERRED_DTE_MIN}-{PREFERRED_DTE_MAX} DTE "
                   f"{direction} contract (liquidity/spread/OI/budget gates)", score=score,
            extras={"direction": direction, "conviction": conviction,
                    "budget": round(budget, 2)},
        )
        return False

    mid_price = contract["_mid_price"]
    option_symbol = contract.get("symbol", "")
    expiration = contract["_expiration"]
    dte = contract["_dte"]
    strike = float(contract.get("strike", 0))
    opt_type = contract["_option_type"]

    quantity = size_contracts(budget, mid_price, risk["equity"], strategy="pead", contract=contract)
    if quantity <= 0:
        log.warning("Budget $%.0f < 1 contract ($%.0f) for %s", budget, mid_price * 100, ticker)
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="pead",
            reason=f"budget ${budget:.0f} ({conviction} sizing) < cost "
                   f"${mid_price * 100:.0f} per contract", score=score,
            extras={"direction": direction, "option_symbol": option_symbol},
        )
        return False

    limit_price = round(mid_price * (1 + LIMIT_BUFFER), 2) if USE_LIMIT else None
    order_type = "limit" if USE_LIMIT else "market"

    log.info("Placing %s | %s $%.0f%s %s (%dDTE) x%d @ $%s", order_type, ticker, strike,
             opt_type[0].upper(), expiration, dte, quantity, limit_price or "mkt")

    fill = place_and_confirm(
        symbol=ticker, option_symbol=option_symbol, side="buy_to_open",
        quantity=quantity, order_type=order_type, price=limit_price, strategy="pead",
        fallback_price=mid_price, timeout=30.0,
    )
    if not (fill.ok and fill.filled_qty > 0):
        log.error("Order did NOT fill for %s: %s", ticker, fill.reason)
        log_trade({"strategy": "pead", "ticker": ticker, "approved": False,
                   "reason": f"order_not_filled: {fill.reason}", "option_symbol": option_symbol})
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="pead",
            reason=f"order not filled ({fill.status}): {fill.reason}", score=score,
            extras={"direction": direction, "option_symbol": option_symbol,
                    "limit_price": limit_price},
        )
        return False

    fill_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else mid_price
    filled_qty = int(fill.filled_qty)

    _add_position({
        "option_symbol": option_symbol, "underlying": ticker, "expiration": expiration,
        "strike": strike, "option_type": opt_type, "direction": direction,
        "entry_price": fill_price, "quantity": filled_qty,
        # ET-anchored, tz-aware so monitor_pead_positions can subtract directly.
        "entry_time": now_est().isoformat(), "setup_score": score,
        "conviction": conviction, "dte": dte, "prev_close": setup.get("prev_close"),
        "event_move": setup.get("event_move"), "order_id": fill.order_id,
    })
    record_trade(ticker)
    log.info("Trade OPEN | %s $%.0f%s x%d @ $%.2f | %dDTE | %s",
             ticker, strike, opt_type[0].upper(), filled_qty, fill_price, dte, conviction)

    # Full audit entry: WHY this trade happened (gap, drift hold, conviction).
    log_decision(
        ticker=ticker, action=ACTION_TRADED, strategy="pead",
        reason=f"score {score} ≥ {MIN_SCAN_SCORE}, {conviction} conviction "
               f"{direction} drift ({setup.get('event_move', '?')}% gap, "
               f"held {setup.get('held_frac', '?')}), filled {filled_qty} @ ${fill_price:.2f}",
        score=score,
        extras={
            "direction":   direction,
            "conviction":  conviction,
            "event_move":  setup.get("event_move"),
            "move_z":      setup.get("move_z"),
            "held_frac":   setup.get("held_frac"),
            "days_since":  setup.get("days_since"),
            "trend":       setup.get("trend"),
            "option_symbol": option_symbol,
            "strike":      strike,
            "expiration":  expiration,
            "dte":         dte,
            "quantity":    filled_qty,
            "entry_price": round(fill_price, 4),
            "cost":        round(filled_qty * fill_price * 100, 2),
            "order_id":    fill.order_id,
        },
        force=True,
    )

    try:
        from mawitek.infra.event_notifier import notify_trade_filled
        notify_trade_filled(
            strategy="pead", ticker=ticker,
            contract=f"${strike:.0f}{opt_type[0].upper()} {expiration} ({dte}DTE)",
            qty=filled_qty, price=fill_price, cost=round(filled_qty * fill_price * 100, 2),
        )
    except Exception as e:
        log.warning("notify_trade_filled failed: %s", e)

    log_trade({"strategy": "pead", "ticker": ticker, "approved": True,
               "reason": "all_checks_passed", "setup_score": score, "direction": direction,
               "conviction": conviction, "option_symbol": option_symbol, "strike": strike,
               "expiration": expiration, "dte": dte, "entry_price": fill_price,
               "quantity": filled_qty, "order_id": fill.order_id,
               "cost_estimate": round(filled_qty * fill_price * 100, 2),
               "equity": risk["equity"], "budget": budget, "event_move": setup.get("event_move")})
    return True


# ─── Market Hours ──────────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    return is_market_open(MARKET_OPEN_H, MARKET_OPEN_M, MARKET_CLOSE_H, MARKET_CLOSE_M)


# ─── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 50)
    log.info("PEAD / NEWS-DRIFT BOT — Strategy 4 — STARTING")
    if MOCK_MODE:
        log.warning("MOCK_MODE — no real orders will be placed")
    log.info("TP +%.0f%% | SL -%.0f%% | max hold %dd | DTE %d-%d",
             TAKE_PROFIT_PCT * 100, STOP_LOSS_PCT * 100, MAX_HOLD_DAYS,
             PREFERRED_DTE_MIN, PREFERRED_DTE_MAX)
    log.info("=" * 50)

    try:
        for r in recover_pending_orders():
            if r.ok and r.filled_qty > 0:
                log.info("Recovered fill from prior session: %s", r.tag)
    except Exception as e:
        log.warning("Pending-order recovery failed (non-fatal): %s", e)

    try:
        n = reconcile_pead_positions()
        if n:
            log.info("Reconciled %d stale PEAD position(s)", n)
    except Exception as e:
        log.warning("PEAD reconciliation failed (non-fatal): %s", e)

    reconcile_from_broker()

    while True:
        market_open = _is_market_open()
        try:
            now_str = now_est().strftime("%H:%M:%S")

            # Scan EVERY cycle, even when the market is closed — PEAD runs on
            # daily bars, so after-hours/weekend scans surface the same drift
            # opportunities for review. Position management + order execution
            # stay gated to market hours (need live option prices).
            beat("pead_executor", status="scanning" if market_open else "scanning_closed")
            log.info("[%s] ── cycle start (%s) ──",
                     now_str, "open" if market_open else "market closed — scan only")

            # 1) manage open positions (only while the market is open)
            if market_open:
                monitor_pead_positions()

            # 2) scan for new drift setups (rotates the liquid universe; own offset)
            setups = run_pead_scan(csv_path=scan_csv(),
                                   universe_limit=SCAN_UNIVERSE_LIMIT,
                                   min_score=MIN_SCAN_SCORE,
                                   rotation_key="pead")

            # Share with the dashboard setups card + alert subscribers
            # (swing candidates). Best-effort — never blocks trading.
            if setups:
                try:
                    from mawitek.dashboard.dashboard_state import _persist_or_restore_setups
                    _persist_or_restore_setups(setups)
                except Exception as e:
                    log.warning("could not persist setups for dashboard: %s", e)
                try:
                    from mawitek.infra.event_notifier import notify_trade_setups
                    notify_trade_setups(setups, style="swing", strategy="pead")
                except Exception as e:
                    log.warning("setup alert failed (non-fatal): %s", e)

            placed = 0
            if market_open:
                for setup in setups:
                    if placed >= MAX_TRADES_PER_SCAN:
                        break
                    if execute_pead_trade(setup):
                        placed += 1
            elif setups:
                log.info("[%s] %d setup(s) found but market closed — surfaced, not traded.",
                         now_str, len(setups))

            log.info("[%s] cycle done | new: %d | open: %d",
                     now_str, placed, len(_load_positions()))

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.exception("Error in main loop: %s — retry in 60s", e)
            time.sleep(60)
            continue

        time.sleep(SCAN_INTERVAL_SEC if market_open else CLOSED_SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PEAD / news-drift executor — Strategy 4")
    parser.add_argument("--show-positions", action="store_true",
                        help="Print tracked PEAD positions and exit")
    args = parser.parse_args()

    if args.show_positions:
        positions = _load_positions()
        if not positions:
            print("No open PEAD positions tracked.")
        else:
            print(f"\n{len(positions)} open PEAD position(s):\n")
            for p in positions:
                print(f"  {p['option_symbol']} | {p['direction']} | "
                      f"Entry ${p['entry_price']:.2f} | x{p['quantity']} | {p.get('conviction')}")
    else:
        run()
