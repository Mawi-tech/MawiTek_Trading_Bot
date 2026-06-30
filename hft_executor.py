"""
hft_executor.py  —  Strategy 3: Intraday Execution Engine

Fast-cycling execution loop for the HFT intraday scanner.
Scans every 60 seconds (configurable), enters on qualifying setups,
and exits on any of:
    - Take profit: +100% on option premium (let convex winners run)
    - Stop loss:   -20% on option premium (cut losers fast)
    - Time stop:   position held > MAX_HOLD_MINUTES
    - EOD flatten: all positions closed 15 minutes before market close

Uses 0-DTE or 1-DTE calls/puts (direction-matched to scanner signal).

Run:
    python hft_executor.py
"""

import time
import datetime

from hft_scanner import run_hft_scan
from universe import scan_csv
import exit_manager
import position_book as _pb
from tradier_client import (
    get_options_expirations, get_options_chain, get_quote,
    get_open_positions, MOCK_MODE,
)
from order_manager import place_and_confirm, recover_pending_orders
from risk_manager import pre_trade_check, size_contracts, record_trade, reconcile_from_broker
from logger import get_logger, log_trade
from trade_journal import record_closed_trade
from decision_log import log_decision, ACTION_TRADED, ACTION_REJECTED, ACTION_EXITED
from heartbeat import beat
from utils import now_est, today_est, parse_isodt, spread_pct as _spread_pct, is_market_open

log = get_logger("hft_executor")


# ─── Execution Config ──────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 60        # Re-scan every 60 seconds
SCAN_INTERVAL       = "5m"      # Bar interval for scanner
MIN_SETUP_SCORE     = 45        # Score floor (50→45 Jun 2026 trade-frequency push)
MAX_TRADES_PER_SCAN = 4         # Max new intraday positions per scan (was 2)
SCAN_UNIVERSE_LIMIT = 250       # Names per scan cycle (rotates through the full market)
                                # (was a stale hardcoded 75 that quietly shrank coverage)

# Exit rules — ASYMMETRIC by design (validated Jun 2026, see backtest_hft.py).
# Long options are convex: the strategy's signals are only ~36% directional, so
# a symmetric 30/25 TP/SL was a coin-flip that bled to costs (PF ~1.0, slightly
# negative). Cutting losers fast (-20%) and letting winners run (+100% or the
# time stop) gives a ~5:1 reward:risk that flips it to PF ~1.6 across two
# independent samples. Win rate FALLS (~45%→36%) but expectancy turns positive.
MAX_HOLD_MINUTES    = 60        # Time-stop: let winners develop (was 45)
TAKE_PROFIT_PCT     = 1.00      # Let winners run to +100% (was 0.30)
STOP_LOSS_PCT       = 0.20      # Cut losers fast at -20% (was 0.25)

# Position size as a fraction of equity, by conviction. "high" = the
# backtest-proven VWAP+ORB+spike trio (full intraday size); "relaxed" = a
# looser-confluence setup (sized DOWN, since its edge is unvalidated).
HFT_SIZE_PCT_HIGH    = 0.010    # 1.0% of equity
HFT_SIZE_PCT_RELAXED = 0.005    # 0.5% of equity

# DTE selection: 0-DTE first, fall back to 1-DTE, then widen out to
# MAX_FALLBACK_DTE for names without weekly options (e.g. PSX has only
# monthlies — on most days it has NO 0-2 DTE chain at all, which used to
# silently kill every setup on such names at contract selection).
# A 3-5 DTE option still scalps fine intraday: less gamma but also less
# theta bleed, and the 60-min time stop / EOD flatten keep it a day trade.
PREFERRED_DTE_MAX   = 1
PREFERRED_DTE_MIN   = 0
MAX_FALLBACK_DTE    = 5

# Order settings
USE_LIMIT           = True
LIMIT_BUFFER        = 0.05      # Cushion ABOVE the ask for a marketable entry limit


def _marketable_limit(ask: float, mid: float, buffer: float = LIMIT_BUFFER) -> float:
    """
    Buy-entry limit that actually FILLS on a fast 0-DTE mover.

    The old code priced at mid×(1+buffer). But the HFT selector allows spreads up
    to 30%, and for any spread wider than ~2×buffer that price sits BELOW the ask,
    so the limit rests unfilled and gets canceled (the day-trade "no fill" bug).
    Pricing off the ASK makes the limit marketable — it crosses the spread and
    fills at the ask (or better), with `buffer` as a small cushion so a tick-up
    between placement and execution still fills. It's a CAP, not the fill price.
    """
    base = ask if ask and ask > 0 else mid
    return round(base * (1 + buffer), 2)

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

# Single-leg book logic is shared across the 3 day/swing executors
# (see position_book.py); these wrappers bind it to this strategy's own file.
def _load_positions() -> list[dict]:
    return _pb.load(HFT_STATE_FILE)


def _save_positions(positions: list[dict]) -> None:
    _pb.save(HFT_STATE_FILE, positions)


def _add_position(position: dict) -> None:
    _pb.add(HFT_STATE_FILE, position)


def _remove_position(option_symbol: str) -> None:
    _pb.remove(HFT_STATE_FILE, option_symbol)


def _update_position(option_symbol: str, **fields) -> None:
    _pb.update(HFT_STATE_FILE, option_symbol, **fields)


def reconcile_hft_positions() -> int:
    """
    Verify each locally-tracked HFT position is still open at the broker.

    The HFT strategy keeps its own position file (hft_positions.json) that the
    main risk_manager reconciliation never touches. Without this, a position
    closed outside the bot (manual close, expiry, broker auto-liquidation)
    would linger in the local file forever — the bot would keep "monitoring"
    a position it no longer owns and could never re-enter the underlying.

    Any local position missing from the broker is journaled as
    closed_externally and dropped from the local file.

    Returns the count of stale positions reconciled.
    """
    local = _load_positions()
    if not local:
        return 0

    try:
        # strict=True → a failed broker read raises instead of returning [],
        # so a transient outage can't make us journal every open position as
        # closed_externally and orphan it from exit management.
        broker_pos  = get_open_positions(strict=True)
        broker_syms = {p.get("symbol") for p in broker_pos if p.get("symbol")}
    except Exception as e:
        log.warning("reconcile_hft_positions: could not query broker: %s", e)
        return 0

    # In MOCK_MODE the broker returns [] — don't nuke local state in that case.
    if MOCK_MODE:
        return 0

    stale = [p for p in local if p.get("option_symbol") not in broker_syms]
    for pos in stale:
        sym = pos.get("option_symbol", "")
        log.info("Stale HFT position %s not at broker — journaling as closed_externally", sym)

        entry_price = float(pos.get("entry_price", 0) or 0)
        # Best-effort exit mark from the chain; fall back to entry (0 P&L).
        exit_price = entry_price
        try:
            chain = get_options_chain(pos.get("underlying", ""), pos.get("expiration", ""))
            for c in chain:
                if c.get("symbol") == sym:
                    bid = float(c.get("bid", 0) or 0)
                    ask = float(c.get("ask", 0) or 0)
                    if bid > 0 and ask > 0:
                        exit_price = round((bid + ask) / 2, 2)
                    break
        except Exception:
            pass

        try:
            record_closed_trade(
                option_symbol = sym,
                underlying    = pos.get("underlying", ""),
                entry_price   = entry_price,
                exit_price    = exit_price,
                quantity      = int(pos.get("quantity", 0) or 0),
                expiration    = pos.get("expiration", ""),
                entry_time    = pos.get("entry_time"),
                exit_reason   = "closed_externally",
                setup_score   = pos.get("setup_score"),
                signals       = {
                    "direction":   pos.get("direction"),
                    "option_type": pos.get("option_type"),
                    "strike":      pos.get("strike"),
                    "dte":         pos.get("dte"),
                },
                strategy      = "hft_intraday",
            )
        except Exception as e:
            log.error("Failed to journal stale HFT position %s: %s", sym, e)
        _remove_position(sym)

    return len(stale)


# ─── Option Selection (0–1 DTE) ────────────────────────────────────────────────

def select_intraday_option(
    ticker: str,
    direction: str,
    budget: float,
) -> dict | None:
    """
    Pick the best contract matching the signal direction: 0-1 DTE preferred,
    widening to MAX_FALLBACK_DTE for names that only carry monthly options.

    direction: "bullish" → call, "bearish" → put.
    Returns best contract dict with _mid_price injected, or None.
    """
    exps = get_options_expirations(ticker)
    if not exps:
        return None

    stock_price = get_quote(ticker)
    if stock_price <= 0:
        return None

    today = today_est()    # ET — the date the broker thinks it is for expiry math
    valid_exps = []
    for exp in exps:
        exp_date = datetime.date.fromisoformat(exp)
        dte = (exp_date - today).days
        if PREFERRED_DTE_MIN <= dte <= PREFERRED_DTE_MAX:
            valid_exps.append((dte, exp))

    if not valid_exps:
        # No 0-1 DTE chain (name without weeklies) — widen out to the nearest
        # expiry within MAX_FALLBACK_DTE so the setup isn't silently dropped.
        for exp in exps:
            exp_date = datetime.date.fromisoformat(exp)
            dte = (exp_date - today).days
            if PREFERRED_DTE_MAX < dte <= MAX_FALLBACK_DTE:
                valid_exps.append((dte, exp))

    if not valid_exps:
        log.warning("No 0-%d DTE expirations for %s", MAX_FALLBACK_DTE, ticker)
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
            # 0-DTE OI is often stale/low at open (settlement day); accept any
            # contract that has a live two-sided market (bid+ask already checked).
            # For 1-2 DTE keep a minimal floor so we're not trading phantom contracts.
            and (dte == 0 or int(c.get("open_interest", 0) or 0) >= 1)
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

    log.warning("No qualifying %s contract for %s", option_type, ticker)
    return None


# ─── Position Monitor ──────────────────────────────────────────────────────────

def monitor_hft_positions():
    """
    Check each tracked HFT position against exit rules:
    - Take profit  (+TAKE_PROFIT_PCT, currently +100% — let convex winners run)
    - Stop loss    (-STOP_LOSS_PCT,   currently  -20% — cut losers fast)
    - Time stop    (held > MAX_HOLD_MINUTES, currently 60m)
    """
    positions = _load_positions()
    if not positions:
        return

    now = now_est()    # tz-aware so it subtracts cleanly from tz-aware entry_time
    to_close = []
    to_scale = []      # (pos, qty) partial scale-outs
    peaks_dirty = False

    for pos in positions:
        option_symbol = pos["option_symbol"]
        entry_price   = pos["entry_price"]
        # parse_isodt tolerates legacy naive entry_time records too.
        entry_time    = parse_isodt(pos["entry_time"])
        quantity      = pos["quantity"]

        # Time stop
        hold_minutes = (now - entry_time).total_seconds() / 60
        if hold_minutes >= MAX_HOLD_MINUTES:
            log.info("Time stop hit for %s (%.0fm held)", option_symbol, hold_minutes)
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

        # Trailing stop + scale-out, layered on top of the fixed TP/SL.
        peak = exit_manager.update_peak(pos, pct_change)
        peaks_dirty = True

        scale_qty = exit_manager.scale_out_quantity(pos, pct_change, exit_manager.HFT_EXIT)
        if scale_qty > 0:
            log.info("Scale-out %s | +%.0f%% — banking %d of %d contracts",
                     option_symbol, pct_change * 100, scale_qty, quantity)
            to_scale.append((pos, scale_qty))
            continue

        if exit_manager.trailing_stop_hit(pct_change, peak, exit_manager.HFT_EXIT):
            log.info("Trailing stop %s | peak +%.0f%% -> now +%.0f%%",
                     option_symbol, peak * 100, pct_change * 100)
            to_close.append((pos, "trailing_stop"))
            continue

        if pct_change >= TAKE_PROFIT_PCT:
            log.info("TP hit %s | Entry $%.2f -> Now $%.2f (+%.1f%%)",
                     option_symbol, entry_price, current_mid, pct_change * 100)
            to_close.append((pos, "take_profit"))

        elif pct_change <= -STOP_LOSS_PCT:
            log.info("SL hit %s | Entry $%.2f -> Now $%.2f (%.1f%%)",
                     option_symbol, entry_price, current_mid, pct_change * 100)
            to_close.append((pos, "stop_loss"))

    # Persist the updated high-water marks before any close reloads the book.
    if peaks_dirty:
        _save_positions(positions)

    for pos, qty in to_scale:
        _close_position(pos, "scale_out", close_qty=qty)
    for pos, reason in to_close:
        _close_position(pos, reason)


def _close_position(pos: dict, reason: str, close_qty: int | None = None):
    """Sell-to-close a tracked HFT position — fully, or just `close_qty` contracts
    for a partial scale-out (the remainder stays open as a house-money runner)."""
    option_symbol = pos["option_symbol"]
    underlying    = pos.get("underlying", pos["option_symbol"])
    entry_price   = pos.get("entry_price", 0)
    quantity      = pos["quantity"]
    qty_to_close  = quantity if close_qty is None else max(1, min(int(close_qty), quantity))
    partial       = qty_to_close < quantity

    log.info("Closing %s x%d%s — reason: %s", option_symbol, qty_to_close,
             " (scale-out)" if partial else "", reason)

    fill = place_and_confirm(
        symbol=underlying,
        option_symbol=option_symbol,
        side="sell_to_close",
        quantity=qty_to_close,
        order_type="market",
        strategy="hft_intraday",
        fallback_price=entry_price,
        timeout=20.0,
    )

    if fill.ok and fill.filled_qty > 0:
        closed_qty = int(fill.filled_qty)
        remaining  = quantity - closed_qty
        if remaining > 0:
            # Partial scale-out — keep the runner, mark it so we only scale once.
            _update_position(option_symbol, quantity=remaining, scaled_out=True)
        else:
            _remove_position(option_symbol)
        exit_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else entry_price
        pnl_pct    = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        pnl_dollar = round((exit_price - entry_price) * closed_qty * 100, 2)
        log.info("Closed %s x%d | Reason: %s | P&L: %+.1f%% ($%+.2f)%s",
                 option_symbol, closed_qty, reason, pnl_pct, pnl_dollar,
                 f" | {remaining} left running" if remaining > 0 else "")
        log_trade({
            "strategy":      "hft_intraday",
            "ticker":        underlying,
            "approved":      True,
            "action":        "sell_to_close",
            "option_symbol": option_symbol,
            "exit_reason":   reason,
            "entry_price":   entry_price,
            "exit_price":    exit_price,
            "quantity":      closed_qty,
            "pnl_pct":       round(pnl_pct, 2),
            "pnl_dollar":    pnl_dollar,
        })
        # Journal to closed_trades.json so the dashboard's Trade History /
        # Analytics tabs can see HFT trades. Previously these only landed
        # in the file log and never reached the journal.
        try:
            record_closed_trade(
                option_symbol = option_symbol,
                underlying    = underlying,
                entry_price   = entry_price,
                exit_price    = exit_price,
                quantity      = closed_qty,
                expiration    = pos.get("expiration", ""),
                entry_time    = pos.get("entry_time"),
                exit_reason   = reason,
                setup_score   = pos.get("setup_score"),
                signals       = {
                    "direction":   pos.get("direction"),
                    "option_type": pos.get("option_type"),
                    "strike":      pos.get("strike"),
                    "dte":         pos.get("dte"),
                },
                strategy      = "hft_intraday",
            )
        except Exception as e:
            log.error("Failed to journal HFT close for %s: %s", option_symbol, e)

        # Audit-log the exit (what fired and the outcome).
        log_decision(
            ticker=underlying, action=ACTION_EXITED, strategy="hft_intraday",
            reason=reason,
            extras={"option_symbol": option_symbol,
                    "entry_price": entry_price, "exit_price": round(exit_price, 4),
                    "pnl_pct": round(pnl_pct, 2), "pnl_dollar": pnl_dollar},
            force=True,   # exits always stay in the audit log
        )

        # Push close notification.
        try:
            from event_notifier import notify_position_closed
            strike   = pos.get("strike", 0)
            opt_type = (pos.get("option_type", "") or "?")[0].upper()
            contract = f"${strike:.0f}{opt_type}"
            notify_position_closed(
                ticker     = underlying,
                contract   = contract,
                pnl_dollar = pnl_dollar,
                pnl_pct    = pnl_pct,
                reason     = reason,
                strategy   = "hft_intraday",
            )
        except Exception as e:
            log.warning("notify_position_closed failed: %s", e)
    else:
        log.error("Close did NOT fill for %s: %s — position left open, will retry next cycle",
                  option_symbol, fill.reason)


def flatten_all_positions():
    """Force-close all HFT positions (called at EOD)."""
    positions = _load_positions()
    if not positions:
        return
    log.info("EOD flatten — closing %d position(s)", len(positions))
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
        log.warning("%s — ambiguous direction '%s', skipping", ticker, direction)
        return False

    log.info("Processing %s | %s | Score: %d", ticker, direction.upper(), score)

    risk = pre_trade_check(ticker, strategy="hft_intraday")
    if not risk["approved"]:
        log.warning("Blocked %s — %s", ticker, risk["reason"])
        log_trade({
            "strategy": "hft", "ticker": ticker, "approved": False,
            "reason": risk["reason"], "setup_score": score, "direction": direction,
            "equity": risk["equity"], "daily_pnl": risk["daily_pnl"],
        })
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="hft_intraday",
            reason=f"risk: {risk['reason']}", score=score,
            extras={"direction": direction, "signals": setup.get("active_signals", []),
                    "daily_pnl": risk.get("daily_pnl")},
        )
        return False

    # Size by conviction: the proven VWAP+ORB+spike trio ("high") gets the full
    # intraday allocation; looser "relaxed" setups are sized down because their
    # edge hasn't been backtest-validated.
    conviction = setup.get("conviction", "relaxed")
    size_pct = HFT_SIZE_PCT_HIGH if conviction == "high" else HFT_SIZE_PCT_RELAXED
    budget = risk["equity"] * size_pct
    log.info("Sizing %s at %.1f%% (%s conviction)", ticker, size_pct * 100, conviction)

    contract = select_intraday_option(ticker, direction, budget)
    if not contract:
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="hft_intraday",
            reason=f"no qualifying 0-{MAX_FALLBACK_DTE} DTE {direction} contract "
                   f"(liquidity/spread/OI gates)", score=score,
            extras={"direction": direction, "conviction": conviction,
                    "budget": round(budget, 2)},
        )
        return False

    mid_price     = contract["_mid_price"]
    option_symbol = contract.get("symbol", "")
    expiration    = contract["_expiration"]
    dte           = contract["_dte"]
    strike        = float(contract.get("strike", 0))
    opt_type      = contract["_option_type"]

    quantity = size_contracts(budget, mid_price, risk["equity"], strategy="hft_intraday", contract=contract)
    if quantity <= 0:
        log.warning("Budget $%.0f < cost of 1 contract ($%.0f)", budget, mid_price * 100)
        log_trade({"strategy": "hft", "ticker": ticker, "approved": False,
                   "reason": "budget_too_small", "setup_score": score,
                   "budget": budget, "entry_price": mid_price})
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="hft_intraday",
            reason=f"budget ${budget:.0f} ({conviction} sizing) < cost "
                   f"${mid_price * 100:.0f} per contract", score=score,
            extras={"direction": direction, "conviction": conviction,
                    "option_symbol": option_symbol, "mid_price": mid_price},
        )
        return False

    # Marketable limit off the ASK (not mid) so fast/wide-spread 0-DTE orders
    # actually fill instead of resting below the ask and getting canceled.
    ask_price   = float(contract.get("ask", 0) or 0)
    limit_price = _marketable_limit(ask_price, mid_price) if USE_LIMIT else None
    order_type  = "limit" if USE_LIMIT else "market"

    log.info(
        "Placing %s | %s $%.0f%s %s (%dDTE) | x%d @ $%s",
        order_type, ticker, strike, opt_type[0].upper(), expiration, dte,
        quantity, limit_price if limit_price else "mkt"
    )

    # Place AND confirm the fill — record the real fill price/qty, not the mid.
    fill = place_and_confirm(
        symbol=ticker,
        option_symbol=option_symbol,
        side="buy_to_open",
        quantity=quantity,
        order_type=order_type,
        price=limit_price,
        strategy="hft_intraday",
        fallback_price=mid_price,
        timeout=20.0,   # intraday — don't wait long on an unfilled limit
    )

    if not fill.ok or fill.filled_qty <= 0:
        log.error("Order did NOT fill for %s: %s", ticker, fill.reason)
        log_trade({"strategy": "hft", "ticker": ticker, "approved": False,
                   "reason": f"order_not_filled ({fill.status}): {fill.reason}",
                   "option_symbol": option_symbol, "setup_score": score})
        log_decision(
            ticker=ticker, action=ACTION_REJECTED, strategy="hft_intraday",
            reason=f"order not filled ({fill.status}): {fill.reason}", score=score,
            extras={"direction": direction, "option_symbol": option_symbol,
                    "limit_price": limit_price},
        )
        return False

    fill_price = float(fill.avg_fill_price) if fill.avg_fill_price > 0 else mid_price
    filled_qty = int(fill.filled_qty)
    if fill.partially_filled:
        log.warning("Partial fill on %s: %d/%d contracts", ticker, filled_qty, quantity)

    # Track the position at the actual fill
    _add_position({
        "option_symbol": option_symbol,
        "underlying":    ticker,
        "expiration":    expiration,
        "strike":        strike,
        "option_type":   opt_type,
        "direction":     direction,
        "entry_price":   fill_price,
        "quantity":      filled_qty,
        # ET-anchored, tz-aware so monitor_hft_positions can subtract directly.
        "entry_time":    now_est().isoformat(),
        "setup_score":   score,
        "dte":           dte,
        "order_id":      fill.order_id,
    })

    record_trade(ticker)
    log.info(
        "Trade OPEN | %s $%.0f%s | x%d @ $%.2f | DTE: %d",
        ticker, strike, opt_type[0].upper(), filled_qty, fill_price, dte
    )

    # Full audit entry: WHY this trade happened (signals, conviction, sizing).
    log_decision(
        ticker=ticker, action=ACTION_TRADED, strategy="hft_intraday",
        reason=f"score {score} ≥ {MIN_SETUP_SCORE}, {conviction} conviction "
               f"{direction}, filled {filled_qty} @ ${fill_price:.2f}",
        score=score,
        extras={
            "direction":     direction,
            "conviction":    conviction,
            "signals":       setup.get("active_signals", []),
            "signal_details": setup.get("signal_details", {}),
            "option_symbol": option_symbol,
            "strike":        strike,
            "expiration":    expiration,
            "dte":           dte,
            "quantity":      filled_qty,
            "entry_price":   round(fill_price, 4),
            "cost":          round(filled_qty * fill_price * 100, 2),
            "size_pct":      size_pct,
            "order_id":      fill.order_id,
        },
        force=True,   # never collapse a real fill out of the audit log
    )

    # Push a fill notification (Telegram / email / Discord).
    try:
        from event_notifier import notify_trade_filled
        notify_trade_filled(
            strategy = "hft_intraday",
            ticker   = ticker,
            contract = f"${strike:.0f}{opt_type[0].upper()} {expiration} ({dte}DTE)",
            qty      = filled_qty,
            price    = fill_price,
            cost     = round(filled_qty * fill_price * 100, 2),
        )
    except Exception as e:
        log.warning("notify_trade_filled failed: %s", e)
    log_trade({
        "strategy":      "hft",
        "ticker":        ticker,
        "approved":      True,
        "reason":        "all_checks_passed",
        "setup_score":   score,
        "direction":     direction,
        "option_symbol": option_symbol,
        "strike":        strike,
        "expiration":    expiration,
        "dte":           dte,
        "entry_price":   fill_price,
        "quantity":      filled_qty,
        "order_type":    order_type,
        "limit_price":   limit_price,
        "order_id":      fill.order_id,
        "cost_estimate": round(filled_qty * fill_price * 100, 2),
        "equity":        risk["equity"],
        "budget":        budget,
        "daily_pnl":     risk["daily_pnl"],
        "signals":       setup.get("active_signals", []),
    })
    return True


# ─── Market Hours Helpers ──────────────────────────────────────────────────────

def _is_market_open() -> bool:
    # HFT's tradeable window ends at the EOD-flatten time, not the 15:55 close.
    return is_market_open(MARKET_OPEN_H, MARKET_OPEN_M, EOD_FLATTEN_H, EOD_FLATTEN_M)


def _is_new_trade_allowed() -> bool:
    """No new entries in the last 30 minutes of the session."""
    now = now_est()
    if now.weekday() >= 5:
        return False
    open_t   = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    cutoff_t = now.replace(hour=CLOSE_SCAN_H,  minute=CLOSE_SCAN_M,  second=0, microsecond=0)
    return open_t <= now <= cutoff_t


def _is_eod_flatten_time() -> bool:
    now = now_est()
    flatten_t = now.replace(hour=EOD_FLATTEN_H, minute=EOD_FLATTEN_M, second=0, microsecond=0)
    close_t   = now.replace(hour=15,            minute=45,             second=0, microsecond=0)
    return flatten_t <= now <= close_t


# ─── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 50)
    log.info("HFT INTRADAY BOT — Strategy 3 — STARTING")
    if MOCK_MODE:
        log.warning("MOCK_MODE — no real orders will be placed")
    log.info("TP: +%.0f%%  SL: -%.0f%%  Time stop: %dm",
             TAKE_PROFIT_PCT * 100, STOP_LOSS_PCT * 100, MAX_HOLD_MINUTES)
    log.info("=" * 50)

    # Resolve any in-flight orders from a prior crash before reconciling state.
    try:
        for r in recover_pending_orders():
            if r.ok and r.filled_qty > 0:
                log.info("Recovered fill from prior session: %s — %s", r.tag, r.reason)
    except Exception as e:
        log.warning("Pending-order recovery failed (non-fatal): %s", e)

    # Drop any HFT positions that were closed at the broker while we were down,
    # so we don't keep monitoring phantom positions.
    try:
        n = reconcile_hft_positions()
        if n:
            log.info("Reconciled %d stale HFT position(s) against broker", n)
    except Exception as e:
        log.warning("HFT position reconciliation failed (non-fatal): %s", e)

    # Reconcile P&L and halt flag from broker in case of a prior crash today.
    reconcile_from_broker()

    while True:
        try:
            now_str = now_est().strftime("%H:%M:%S")

            # HFT is intrinsically an INTRADAY strategy (5-minute bars + the
            # prime-session gate), so there are no day-trade setups when the
            # market is closed — unlike the swing scanners (catalyst/PEAD/bounce)
            # which run on daily data and scan around the clock. So this loop
            # stays idle after hours rather than burning API calls on stale bars.
            if not _is_market_open():
                log.debug("[%s] Market closed — sleeping 60s", now_str)
                beat("hft_executor", status="idle")
                time.sleep(60)
                continue

            # EOD: force flatten then sleep until next session
            if _is_eod_flatten_time():
                beat("hft_executor", status="eod_flatten")
                flatten_all_positions()
                log.info("[%s] EOD flatten done — sleeping 30m", now_str)
                time.sleep(1800)
                continue

            log.info("[%s] ── cycle start ──", now_str)
            beat("hft_executor", status="scanning")

            # Step 1: monitor and exit open positions
            monitor_hft_positions()

            # Step 2: open new trades only if time allows
            if _is_new_trade_allowed():
                setups = run_hft_scan(
                    csv_path=scan_csv(),
                    interval=SCAN_INTERVAL,
                    min_score=MIN_SETUP_SCORE,
                    universe_limit=SCAN_UNIVERSE_LIMIT,
                    rotation_key="hft",
                )

                # Surface day-trade candidates beyond this process: merge them
                # into the shared scanner-setups list (dashboard Overview card,
                # tagged trade_style="day") and alert subscribers (deduped per
                # ticker per day). Neither may ever block trading.
                if setups:
                    try:
                        from dashboard_state import _persist_or_restore_setups
                        _persist_or_restore_setups(setups)
                    except Exception as e:
                        log.warning("could not persist day setups for dashboard: %s", e)
                    try:
                        from event_notifier import notify_trade_setups
                        notify_trade_setups(setups, style="day", strategy="hft_intraday")
                    except Exception as e:
                        log.warning("setup alert failed (non-fatal): %s", e)

                trades_placed = 0
                for setup in setups:
                    if trades_placed >= MAX_TRADES_PER_SCAN:
                        break
                    if execute_hft_trade(setup):
                        trades_placed += 1

                log.info("[%s] Cycle done | New trades: %d | Open positions: %d",
                         now_str, trades_placed, len(_load_positions()))
            else:
                log.debug("[%s] Past new-trade cutoff — monitoring only", now_str)

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
                held = (now_est() - parse_isodt(p["entry_time"])).total_seconds() / 60
                print(
                    f"  {p['option_symbol']} | {p['direction']} | "
                    f"Entry: ${p['entry_price']:.2f} | "
                    f"x{p['quantity']} | Held: {held:.0f}m"
                )
    else:
        run()
