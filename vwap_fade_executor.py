"""
vwap_fade_executor.py  —  Strategy 6: VWAP Mean-Reversion Fade execution engine

Execution loop for the VWAP-fade scanner (vwap_fade_scanner.py). Enters short-dated
options that fade an over-extension back toward VWAP, and exits on:
    - VWAP-touch TP:   underlying reverts to within VWAP_TOUCH_BAND_PCT of VWAP
                       (the thesis is complete) — closes regardless of premium P&L
    - Premium TP:      +TAKE_PROFIT_PCT on the option
    - Premium SL:      -STOP_LOSS_PCT on the option
    - Further-stretch: underlying accelerates STRETCH_STOP_PCT further from VWAP
                       past entry (the fade is being run over) — hard invalidation
    - Time stop:       held > MAX_HOLD_MINUTES (reversion is quick; if it hasn't
                       started, the thesis is dead and 0-DTE theta is bleeding)
    - EOD flatten:     all positions closed before the bell

Reuses the HFT executor's contract selection + marketable-limit helpers and the
shared position-book / exit-manager plumbing — only the fade-specific monitor
differs. Guarded by ENABLE_VWAP_FADE: while False the loop idles, so the process
is safe to register in start_all.py before the backtest validates the edge.

Run:
    python vwap_fade_executor.py
"""

import time
import datetime

import exit_manager
import position_book as _pb
import vwap_fade_scanner
from vwap_fade_scanner import run_vwap_fade_scan, fetch_intraday, compute_vwap
from hft_executor import select_intraday_option, _marketable_limit, USE_LIMIT
from universe import scan_csv
from tradier_client import get_options_chain, get_quote, get_open_positions, MOCK_MODE
from order_manager import place_and_confirm, recover_pending_orders
from state_io import StateCorruption, abort_on_corruption
from risk_manager import pre_trade_check, size_contracts, record_trade, reconcile_from_broker
from logger import get_logger, log_trade
from trade_journal import record_closed_trade
from decision_log import log_decision, ACTION_TRADED, ACTION_REJECTED, ACTION_EXITED
from heartbeat import beat
from utils import now_est, parse_isodt, is_market_open

log = get_logger("vwap_fade_executor")

STRATEGY = "vwap_fade"


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 60        # Re-scan every 60 seconds
SCAN_INTERVAL       = "5m"      # Bar interval for the scanner
MIN_SETUP_SCORE     = 50        # Score floor to enter (fades want a real extreme)
MAX_TRADES_PER_SCAN = 3
SCAN_UNIVERSE_LIMIT = 250

# Exit rules — reversion-tuned (tighter/faster than the HFT momentum book, whose
# convex winners run to +100% over 60 min). A fade targets a quick snap back to
# VWAP: bank the smaller, faster move and get out if it doesn't come.
MAX_HOLD_MINUTES    = 30        # half of HFT's 60 — reversion is quick or wrong
TAKE_PROFIT_PCT     = 0.40      # +40% on premium
STOP_LOSS_PCT       = 0.25      # -25% on premium
VWAP_TOUCH_BAND_PCT = 0.10      # underlying within 0.10% of VWAP → thesis complete
STRETCH_STOP_PCT    = 0.50      # accelerates 0.50% further from VWAP past entry → invalidated

# Position size as a fraction of equity, by conviction (same pattern as HFT).
FADE_SIZE_PCT_HIGH    = 0.010   # 1.0% of equity
FADE_SIZE_PCT_RELAXED = 0.005   # 0.5% of equity

# Market hours (ET) — mirror HFT; the fade scanner's own 10:00–14:30 window sits
# inside this, and the new-trade cutoff keeps the 30-min stop before the flatten.
MARKET_OPEN_H, MARKET_OPEN_M = 9, 35
CLOSE_SCAN_H,  CLOSE_SCAN_M  = 15, 0
EOD_FLATTEN_H, EOD_FLATTEN_M = 15, 15

VWAP_FADE_STATE_FILE = "vwap_fade_positions.json"


# ─── Position State ────────────────────────────────────────────────────────────

def _load_positions() -> list[dict]:
    return _pb.load(VWAP_FADE_STATE_FILE)


def _save_positions(positions: list[dict]) -> None:
    _pb.save(VWAP_FADE_STATE_FILE, positions)


def _add_position(position: dict) -> None:
    _pb.add(VWAP_FADE_STATE_FILE, position)


def _remove_position(option_symbol: str) -> None:
    _pb.remove(VWAP_FADE_STATE_FILE, option_symbol)


def _update_position(option_symbol: str, **fields) -> None:
    _pb.update(VWAP_FADE_STATE_FILE, option_symbol, **fields)


def reconcile_vwap_fade_positions() -> int:
    """Drop locally-tracked positions no longer open at the broker (mirrors
    reconcile_hft_positions). Journals them closed_externally. No-op in MOCK_MODE."""
    local = _load_positions()
    if not local:
        return 0
    try:
        broker_syms = {p.get("symbol") for p in get_open_positions(strict=True) if p.get("symbol")}
    except Exception as e:
        log.warning("reconcile: could not query broker: %s", e)
        return 0
    if MOCK_MODE:
        return 0

    stale = [p for p in local if p.get("option_symbol") not in broker_syms]
    for pos in stale:
        sym = pos.get("option_symbol", "")
        log.info("Stale %s position %s not at broker — journaling closed_externally", STRATEGY, sym)
        entry_price = float(pos.get("entry_price", 0) or 0)
        try:
            record_closed_trade(
                option_symbol=sym, underlying=pos.get("underlying", ""),
                entry_price=entry_price, exit_price=entry_price,
                quantity=int(pos.get("quantity", 0) or 0),
                expiration=pos.get("expiration", ""), entry_time=pos.get("entry_time"),
                exit_reason="closed_externally", setup_score=pos.get("setup_score"),
                signals={"direction": pos.get("direction"), "option_type": pos.get("option_type"),
                         "strike": pos.get("strike"), "dte": pos.get("dte")},
                strategy=STRATEGY,
            )
        except Exception as e:
            log.error("Failed to journal stale position %s: %s", sym, e)
        _remove_position(sym)
    return len(stale)


# ─── Reversion helper ──────────────────────────────────────────────────────────

def _current_vwap_context(underlying: str) -> tuple[float, float] | None:
    """(current_price, current_vwap) for the underlying from fresh intraday bars,
    or None if unavailable. Used by the monitor's reversion-based exits."""
    try:
        df = fetch_intraday(underlying, interval=SCAN_INTERVAL)
        if df.empty or len(df) < 2:
            return None
        vwap = compute_vwap(df)
        price = float(df["Close"].iloc[-1])
        vw = float(vwap.iloc[-1])
        if price <= 0 or vw <= 0:
            return None
        return price, vw
    except Exception as e:
        log.debug("vwap context fetch failed for %s: %s", underlying, e)
        return None


# ─── Position Monitor ──────────────────────────────────────────────────────────

def monitor_vwap_fade_positions():
    """Check each tracked fade position against its exit rules (reversion-first)."""
    positions = _load_positions()
    if not positions:
        return

    now = now_est()
    to_close, to_scale = [], []
    peaks_dirty = False

    for pos in positions:
        option_symbol = pos["option_symbol"]
        entry_price   = pos["entry_price"]
        entry_time    = parse_isodt(pos["entry_time"])
        quantity      = pos["quantity"]

        # Time stop — reversion is quick or the thesis is wrong.
        hold_minutes = (now - entry_time).total_seconds() / 60
        if hold_minutes >= MAX_HOLD_MINUTES:
            log.info("Time stop %s (%.0fm held)", option_symbol, hold_minutes)
            to_close.append((pos, "time_stop"))
            continue

        # Reversion checks on the UNDERLYING (independent of premium marks) —
        # skipped in MOCK_MODE (no live bars).
        if not MOCK_MODE:
            ctx = _current_vwap_context(pos.get("underlying", ""))
            if ctx:
                price, vwap = ctx
                pct_vs_vwap = (price - vwap) / vwap * 100
                # VWAP-touch TP — reverted to fair value, thesis complete.
                if abs(pct_vs_vwap) <= VWAP_TOUCH_BAND_PCT:
                    log.info("VWAP-touch TP %s | underlying %.2f ~ VWAP %.2f", option_symbol, price, vwap)
                    to_close.append((pos, "vwap_touch"))
                    continue
                # Further-stretch invalidation — the move is accelerating AWAY
                # from VWAP past entry (a trend running the fade over). Fire on
                # price, before delta+theta compound the premium loss.
                entry_pct = float(pos.get("entry_pct_vs_vwap", 0.0) or 0.0)
                if entry_pct != 0 and (pct_vs_vwap - entry_pct) * (1 if entry_pct > 0 else -1) >= STRETCH_STOP_PCT:
                    log.info("Further-stretch invalidation %s | entry %.2f%% → now %.2f%% vs VWAP",
                             option_symbol, entry_pct, pct_vs_vwap)
                    to_close.append((pos, "further_stretch"))
                    continue

        # Premium-based exits need a live option mark — skip in MOCK_MODE.
        if MOCK_MODE:
            continue
        underlying = pos.get("underlying", "")
        expiration = pos.get("expiration", "")
        if not underlying or not expiration:
            continue
        chain = get_options_chain(underlying, expiration)
        contract = next((c for c in chain if c.get("symbol") == option_symbol), None)
        if not contract:
            continue
        bid = float(contract.get("bid", 0) or 0)
        ask = float(contract.get("ask", 0) or 0)
        if bid <= 0:
            continue
        current_mid = (bid + ask) / 2
        pct_change = (current_mid - entry_price) / entry_price

        peak = exit_manager.update_peak(pos, pct_change)
        peaks_dirty = True

        scale_qty = exit_manager.scale_out_quantity(pos, pct_change, exit_manager.VWAP_FADE_EXIT)
        if scale_qty > 0:
            log.info("Scale-out %s | +%.0f%% — banking %d of %d", option_symbol, pct_change * 100, scale_qty, quantity)
            to_scale.append((pos, scale_qty))
            continue
        if exit_manager.trailing_stop_hit(pct_change, peak, exit_manager.VWAP_FADE_EXIT):
            log.info("Trailing stop %s | peak +%.0f%% -> +%.0f%%", option_symbol, peak * 100, pct_change * 100)
            to_close.append((pos, "trailing_stop"))
            continue
        if pct_change >= TAKE_PROFIT_PCT:
            to_close.append((pos, "take_profit"))
        elif pct_change <= -STOP_LOSS_PCT:
            to_close.append((pos, "stop_loss"))

    if peaks_dirty:
        _save_positions(positions)
    for pos, qty in to_scale:
        _close_position(pos, "scale_out", close_qty=qty)
    for pos, reason in to_close:
        _close_position(pos, reason)


def _close_position(pos: dict, reason: str, close_qty: int | None = None):
    """Sell-to-close a tracked fade position (fully, or a partial scale-out)."""
    option_symbol = pos["option_symbol"]
    underlying    = pos.get("underlying", option_symbol)
    entry_price   = pos.get("entry_price", 0)
    quantity      = pos["quantity"]
    qty_to_close  = quantity if close_qty is None else max(1, min(int(close_qty), quantity))

    fill = place_and_confirm(
        symbol=underlying, option_symbol=option_symbol, side="sell_to_close",
        quantity=qty_to_close, order_type="market", strategy=STRATEGY,
        fallback_price=entry_price, timeout=20.0,
    )
    if not (fill.ok and fill.filled_qty > 0):
        log.error("Close did NOT fill for %s: %s — left open, retry next cycle", option_symbol, fill.reason)
        return

    closed_qty = int(fill.filled_qty)
    remaining  = quantity - closed_qty
    if remaining > 0:
        _update_position(option_symbol, quantity=remaining, scaled_out=True)
    else:
        _remove_position(option_symbol)
    exit_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else entry_price
    pnl_pct    = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
    pnl_dollar = round((exit_price - entry_price) * closed_qty * 100, 2)
    log.info("Closed %s x%d | %s | P&L: %+.1f%% ($%+.2f)%s", option_symbol, closed_qty, reason,
             pnl_pct, pnl_dollar, f" | {remaining} running" if remaining > 0 else "")
    log_trade({"strategy": STRATEGY, "ticker": underlying, "approved": True,
               "action": "sell_to_close", "option_symbol": option_symbol, "exit_reason": reason,
               "entry_price": entry_price, "exit_price": exit_price, "quantity": closed_qty,
               "pnl_pct": round(pnl_pct, 2), "pnl_dollar": pnl_dollar})
    try:
        record_closed_trade(
            option_symbol=option_symbol, underlying=underlying, entry_price=entry_price,
            exit_price=exit_price, quantity=closed_qty, expiration=pos.get("expiration", ""),
            entry_time=pos.get("entry_time"), exit_reason=reason, setup_score=pos.get("setup_score"),
            signals={"direction": pos.get("direction"), "option_type": pos.get("option_type"),
                     "strike": pos.get("strike"), "dte": pos.get("dte")},
            strategy=STRATEGY,
        )
    except Exception as e:
        log.error("Failed to journal %s close for %s: %s", STRATEGY, option_symbol, e)
    log_decision(ticker=underlying, action=ACTION_EXITED, strategy=STRATEGY, reason=reason,
                 extras={"option_symbol": option_symbol, "entry_price": entry_price,
                         "exit_price": round(exit_price, 4), "pnl_pct": round(pnl_pct, 2),
                         "pnl_dollar": pnl_dollar}, force=True)
    try:
        from event_notifier import notify_position_closed
        opt_type = (pos.get("option_type", "") or "?")[0].upper()
        notify_position_closed(
            ticker=underlying, contract=f"${pos.get('strike', 0):.0f}{opt_type}",
            pnl_dollar=pnl_dollar, pnl_pct=pnl_pct, reason=reason, strategy=STRATEGY,
            entry_time=pos.get("entry_time"),
        )
    except Exception as e:
        log.warning("notify_position_closed failed: %s", e)


def flatten_all_positions():
    """Force-close all fade positions (EOD)."""
    positions = _load_positions()
    if not positions:
        return
    log.info("EOD flatten — closing %d position(s)", len(positions))
    for pos in positions:
        _close_position(pos, "eod_flatten")


# ─── Trade Entry ───────────────────────────────────────────────────────────────

def execute_vwap_fade_trade(setup: dict) -> bool:
    """Open one fade position for a qualifying setup. Returns True if placed."""
    ticker    = setup["ticker"]
    direction = setup.get("direction", "")
    score     = setup.get("setup_score", 0)
    if direction not in ("bullish", "bearish"):
        log.warning("%s — ambiguous direction '%s', skipping", ticker, direction)
        return False

    risk = pre_trade_check(ticker, strategy=STRATEGY)
    if not risk["approved"]:
        log.warning("Blocked %s — %s", ticker, risk["reason"])
        log_decision(ticker=ticker, action=ACTION_REJECTED, strategy=STRATEGY,
                     reason=f"risk: {risk['reason']}", score=score,
                     extras={"direction": direction, "daily_pnl": risk.get("daily_pnl")})
        return False

    conviction = setup.get("conviction", "relaxed")
    size_pct = FADE_SIZE_PCT_HIGH if conviction == "high" else FADE_SIZE_PCT_RELAXED
    budget = risk["equity"] * size_pct

    contract = select_intraday_option(ticker, direction, budget)
    if not contract:
        log_decision(ticker=ticker, action=ACTION_REJECTED, strategy=STRATEGY,
                     reason=f"no qualifying {direction} contract (liquidity/spread/OI)",
                     score=score, extras={"direction": direction, "budget": round(budget, 2)})
        return False

    mid_price     = contract["_mid_price"]
    option_symbol = contract.get("symbol", "")
    expiration    = contract["_expiration"]
    dte           = contract["_dte"]
    strike        = float(contract.get("strike", 0))
    opt_type      = contract["_option_type"]

    quantity = size_contracts(budget, mid_price, risk["equity"], strategy=STRATEGY, contract=contract)
    if quantity <= 0:
        log_decision(ticker=ticker, action=ACTION_REJECTED, strategy=STRATEGY,
                     reason=f"budget ${budget:.0f} < cost ${mid_price * 100:.0f}/contract",
                     score=score, extras={"direction": direction, "option_symbol": option_symbol})
        return False

    ask_price   = float(contract.get("ask", 0) or 0)
    limit_price = _marketable_limit(ask_price, mid_price) if USE_LIMIT else None
    order_type  = "limit" if USE_LIMIT else "market"

    fill = place_and_confirm(
        symbol=ticker, option_symbol=option_symbol, side="buy_to_open",
        quantity=quantity, order_type=order_type, price=limit_price, strategy=STRATEGY,
        fallback_price=mid_price, timeout=20.0,
    )
    if not (fill.ok and fill.filled_qty > 0):
        log.error("Order did NOT fill for %s: %s", ticker, fill.reason)
        log_decision(ticker=ticker, action=ACTION_REJECTED, strategy=STRATEGY,
                     reason=f"order not filled ({fill.status}): {fill.reason}", score=score,
                     extras={"direction": direction, "option_symbol": option_symbol})
        return False

    fill_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else mid_price
    filled_qty = int(fill.filled_qty)

    _add_position({
        "option_symbol": option_symbol, "underlying": ticker, "expiration": expiration,
        "strike": strike, "option_type": opt_type, "direction": direction,
        "entry_price": fill_price, "quantity": filled_qty,
        "entry_time": now_est().isoformat(), "setup_score": score, "dte": dte,
        "order_id": fill.order_id,
        # Fade-specific: remember where VWAP was so the monitor can detect a
        # reversion (VWAP-touch) or a further-stretch invalidation.
        "entry_vwap":         setup.get("vwap"),
        "entry_pct_vs_vwap":  setup.get("pct_vs_vwap"),
        "conviction":         conviction,
    })
    record_trade(ticker)
    log.info("Trade OPEN | %s $%.0f%s | x%d @ $%.2f | %dDTE | %s fade",
             ticker, strike, opt_type[0].upper(), filled_qty, fill_price, dte, direction)
    log_decision(ticker=ticker, action=ACTION_TRADED, strategy=STRATEGY,
                 reason=f"score {score} ≥ {MIN_SETUP_SCORE}, {conviction} {direction} VWAP fade, "
                        f"filled {filled_qty} @ ${fill_price:.2f}",
                 score=score, extras={
                     "direction": direction, "conviction": conviction,
                     "signals": setup.get("active_signals", []), "option_symbol": option_symbol,
                     "strike": strike, "expiration": expiration, "dte": dte, "quantity": filled_qty,
                     "entry_price": round(fill_price, 4), "stretch_z": setup.get("stretch_z"),
                     "pct_vs_vwap": setup.get("pct_vs_vwap"), "size_pct": size_pct,
                     "order_id": fill.order_id}, force=True)
    try:
        from event_notifier import notify_trade_filled
        notify_trade_filled(strategy=STRATEGY, ticker=ticker,
                            contract=f"${strike:.0f}{opt_type[0].upper()} {expiration} ({dte}DTE)",
                            qty=filled_qty, price=fill_price, cost=round(filled_qty * fill_price * 100, 2))
    except Exception as e:
        log.warning("notify_trade_filled failed: %s", e)
    return True


# ─── Market Hours Helpers ──────────────────────────────────────────────────────

def _is_market_open() -> bool:
    return is_market_open(MARKET_OPEN_H, MARKET_OPEN_M, EOD_FLATTEN_H, EOD_FLATTEN_M)


def _is_new_trade_allowed() -> bool:
    now = now_est()
    if now.weekday() >= 5:
        return False
    open_t   = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    cutoff_t = now.replace(hour=CLOSE_SCAN_H,  minute=CLOSE_SCAN_M,  second=0, microsecond=0)
    return open_t <= now <= cutoff_t


def _is_eod_flatten_time() -> bool:
    now = now_est()
    flatten_t = now.replace(hour=EOD_FLATTEN_H, minute=EOD_FLATTEN_M, second=0, microsecond=0)
    close_t   = now.replace(hour=15, minute=45, second=0, microsecond=0)
    return flatten_t <= now <= close_t


# ─── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 50)
    log.info("VWAP MEAN-REVERSION FADE — Strategy 6 — STARTING")
    if not vwap_fade_scanner.ENABLE_VWAP_FADE:
        log.warning("ENABLE_VWAP_FADE=False — strategy is DARK; loop will idle. "
                    "Validate via backtest_vwap_fade.py, then flip the flag.")
    if MOCK_MODE:
        log.warning("MOCK_MODE — no real orders will be placed")
    log.info("TP: +%.0f%%  SL: -%.0f%%  Time stop: %dm  VWAP-touch: %.2f%%",
             TAKE_PROFIT_PCT * 100, STOP_LOSS_PCT * 100, MAX_HOLD_MINUTES, VWAP_TOUCH_BAND_PCT)
    log.info("=" * 50)

    try:
        for r in recover_pending_orders():
            if r.ok and r.filled_qty > 0:
                log.info("Recovered fill from prior session: %s — %s", r.tag, r.reason)
    except StateCorruption as e:
        # Must precede the catch-all: an unreadable pending ledger means we
        # cannot know what is in flight at the broker.
        abort_on_corruption(e, "vwap_fade_executor")
    except Exception as e:
        log.warning("Pending-order recovery failed (non-fatal): %s", e)
    try:
        n = reconcile_vwap_fade_positions()
        if n:
            log.info("Reconciled %d stale position(s)", n)
    except Exception as e:
        log.warning("Position reconciliation failed (non-fatal): %s", e)
    reconcile_from_broker()

    while True:
        try:
            now_str = now_est().strftime("%H:%M:%S")

            # DARK mode: stay registered but do nothing until the flag flips.
            # Beat well within the watchdog's stall grace (150s) so a dark process
            # doesn't look hung.
            if not vwap_fade_scanner.ENABLE_VWAP_FADE:
                beat("vwap_fade_executor", status="disabled")
                time.sleep(120)
                continue

            if not _is_market_open():
                beat("vwap_fade_executor", status="idle")
                time.sleep(60)
                continue

            if _is_eod_flatten_time():
                beat("vwap_fade_executor", status="eod_flatten")
                flatten_all_positions()
                log.info("[%s] EOD flatten done — sleeping 30m", now_str)
                time.sleep(1800)
                continue

            beat("vwap_fade_executor", status="scanning")
            monitor_vwap_fade_positions()

            if _is_new_trade_allowed():
                setups = run_vwap_fade_scan(
                    csv_path=scan_csv(), interval=SCAN_INTERVAL,
                    min_score=MIN_SETUP_SCORE, universe_limit=SCAN_UNIVERSE_LIMIT,
                    rotation_key="vwap_fade",
                )
                if setups:
                    try:
                        from dashboard_state import _persist_or_restore_setups
                        _persist_or_restore_setups(setups)
                    except Exception as e:
                        log.warning("could not persist day setups for dashboard: %s", e)
                    try:
                        from event_notifier import notify_trade_setups
                        notify_trade_setups(setups, style="day", strategy=STRATEGY)
                    except Exception as e:
                        log.warning("setup alert failed (non-fatal): %s", e)

                placed = 0
                for setup in setups:
                    if placed >= MAX_TRADES_PER_SCAN:
                        break
                    if execute_vwap_fade_trade(setup):
                        placed += 1
                log.info("[%s] Cycle done | New: %d | Open: %d", now_str, placed, len(_load_positions()))

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.exception("Error in main loop: %s — retrying in 30s", e)
            time.sleep(30)
            continue

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="VWAP Mean-Reversion Fade Executor — Strategy 6")
    parser.add_argument("--flatten", action="store_true", help="Flatten all open fade positions and exit")
    parser.add_argument("--show-positions", action="store_true", help="Print tracked positions and exit")
    args = parser.parse_args()

    if args.flatten:
        flatten_all_positions()
    elif args.show_positions:
        for p in _load_positions():
            print(p)
    else:
        run()
