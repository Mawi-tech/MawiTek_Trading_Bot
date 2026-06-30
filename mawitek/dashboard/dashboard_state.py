"""
dashboard_state.py

Writes bot state to dashboard_state.json after every scan cycle.
The dashboard reads this file to show real live data.

Exports:
- Account balances
- Open positions with live P&L
- Today's trades
- Latest scanner setups
- Risk state (daily P&L, halt status)
- Bot status
"""

import json
import datetime
import os
import re
import time

from mawitek.data.tradier_client import (
    get_account_balance, get_open_positions, get_orders_today,
    get_option_mid, get_gainloss,
)
from mawitek.core.risk_manager import (
    load_state, DAILY_LOSS_LIMIT_PCT, MAX_OPEN_POSITIONS,
    MAX_SWING_POSITIONS, MAX_DAY_POSITIONS, DAY_TRADE_MAX_DTE,
    MAX_PORTFOLIO_VEGA_PCT as VEGA_LIMIT_PCT,
    drawdown_status,
)
from mawitek.core.position_manager import load_positions, days_until_expiry
from mawitek.core.trade_journal import load_closed_trades
from mawitek.core.decision_log import load_recent_decisions
from mawitek.core.equity_tracker import load_equity_curve
from mawitek.infra.state_io import atomic_write_json, read_json, file_lock
from mawitek.analysis.analytics_metrics import compute_metrics
from mawitek.infra.utils import now_est, today_est


def _parse_occ_symbol(symbol: str) -> dict | None:
    """
    Parse a standard OCC option symbol into its components.
    Format: <UNDERLYING><YYMMDD><C|P><8-digit-strike>
    Example: QCOM260618P00220000 → underlying=QCOM, expiry=2026-06-18, type=P, strike=220.0
    Returns None if the symbol doesn't match.
    """
    m = re.match(r'^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$', symbol)
    if not m:
        return None
    underlying, yy, mm, dd, opt_type, strike_raw = m.groups()
    expiry = f"20{yy}-{mm}-{dd}"
    strike = int(strike_raw) / 1000.0
    return {"underlying": underlying, "expiry": expiry, "type": opt_type, "strike": strike}

STATE_FILE = "dashboard_state.json"
SCANNER_SETUPS_FILE = "scanner_setups.json"
# Cap the accumulated list so the on-disk file can't grow without bound. This is
# a STORAGE limit, not a time limit — setups are never expired by age. When the
# list is over the cap the OLDEST-seen entries are dropped first (least relevant)
# while every recently-seen opportunity is kept. Raised well above the old value
# because the list now accumulates opportunities from across the whole market.
SCANNER_SETUPS_MAX  = 300


def _persist_or_restore_setups(setups: list[dict] | None) -> tuple[list[dict], str | None]:
    """
    Maintain an ACCUMULATING board of scanner setups, shared by every scanner
    (catalyst, HFT day-trades, PEAD, bounce — each tags its setups with
    `trade_style` and a human-readable `style_reason`).

    Setups are NEVER deleted by a timer — once found, an opportunity stays on the
    board with the date/time it was first found, so you can review what the
    scanner surfaced even days later (including after-hours / weekend finds).
    Each scan MERGES its results into the saved list rather than replacing it:
      • a ticker seen again has its data refreshed (newest scan wins), its
        `last_seen` timestamp bumped, and `first_seen` / `found_at` preserved
      • new tickers are added with `first_seen` = `found_at` = now
      • an empty/idle cycle deletes nothing
      • the list is sorted most-recently-seen first (then by score) so live
        setups sit on top and historical ones trail; only when it exceeds
        SCANNER_SETUPS_MAX are the oldest-seen trimmed (a pure storage bound)

    Cross-process safe: the catalyst executor AND the hft/pead/bounce
    executors all merge into this file, so the read-merge-write runs under
    a file lock (otherwise two simultaneous merges lose one side's setups).

    Returns (accumulated_setups, latest_update_iso).
    """
    with file_lock(SCANNER_SETUPS_FILE):
        saved    = read_json(SCANNER_SETUPS_FILE, {})
        existing = saved.get("setups", []) if isinstance(saved, dict) else []
        last_ts  = saved.get("timestamp") if isinstance(saved, dict) else None

        if not setups:
            # Idle/scanning cycle — keep the full accumulated board untouched.
            return existing, last_ts

        now = now_est().isoformat()    # ET-anchored "found"/"last seen" timestamps

        by_ticker: dict[str, dict] = {}
        for s in existing:
            t = s.get("ticker")
            if t:
                by_ticker[t] = s

        for s in setups:
            t = s.get("ticker")
            if not t:
                continue
            merged = dict(s)
            prior = by_ticker.get(t, {})
            # `first_seen`/`found_at` mark when the opportunity was FIRST found and
            # are preserved across refreshes; `last_seen` is the most recent scan.
            merged["first_seen"] = prior.get("first_seen") or prior.get("found_at") or now
            merged["found_at"]   = merged["first_seen"]
            merged["last_seen"]  = now
            by_ticker[t] = merged

        # Most-recently-seen first (live setups on top), score as the tiebreaker.
        accumulated = sorted(
            by_ticker.values(),
            key=lambda s: (s.get("last_seen") or "", s.get("setup_score", 0)),
            reverse=True,
        )
        accumulated = accumulated[:SCANNER_SETUPS_MAX]

        try:
            atomic_write_json(SCANNER_SETUPS_FILE, {"timestamp": now, "setups": accumulated})
        except Exception as e:
            print(f"[Dashboard] Could not persist scanner setups: {e}")

    return accumulated, now




def _classify_position_type(legs: list[dict]) -> str:
    """
    Determine the position type from a group of option legs on the same
    underlying + expiration.

    Returns a human-readable label: "Long Call", "Bear Put Spread",
    "Iron Condor", etc.
    """
    if not legs:
        return "Unknown"

    long_legs  = [l for l in legs if l["quantity"] > 0]
    short_legs = [l for l in legs if l["quantity"] < 0]

    # Single leg
    if len(legs) == 1:
        leg = legs[0]
        side = "Long" if leg["quantity"] > 0 else "Short"
        return f"{side} {leg['opt_type_label']}"

    # All calls or all puts?
    types = set(l["opt_type"] for l in legs)
    all_calls = types == {"C"}
    all_puts  = types == {"P"}

    # 2-leg vertical spread
    if len(legs) == 2 and len(long_legs) == 1 and len(short_legs) == 1:
        lo = long_legs[0]
        sh = short_legs[0]
        if all_calls:
            if lo["strike"] < sh["strike"]:
                return "Bull Call Spread"
            else:
                return "Bear Call Spread"
        if all_puts:
            if lo["strike"] < sh["strike"]:
                return "Bull Put Spread"
            else:
                return "Bear Put Spread"
        # Mixed put/call 2-leg: straddle, strangle, or synthetic
        return "Combo (2-leg)"

    # 3+ legs
    if len(legs) == 4 and len(long_legs) == 2 and len(short_legs) == 2:
        if not all_calls and not all_puts:
            return "Iron Condor / Iron Butterfly"
        return "Butterfly / Condor"

    # Ratio spreads (unequal long/short quantities)
    if len(long_legs) >= 1 and len(short_legs) >= 1:
        return "Ratio / Complex Spread"

    # Fallback
    return f"Multi-leg ({len(legs)} legs)"


def build_positions_data() -> tuple[list[dict], int]:
    """
    Build enriched position data with live P&L, grouped into logical
    positions (spreads have their legs nested).

    Returns:
        (positions_list, position_count)

    position_count is the number of distinct positions (a spread counts
    as ONE, not as N legs). Use this for the dashboard metric card instead
    of len(positions_list).
    """
    from mawitek.core.position_manager import TAKE_PROFIT_PCT, STOP_LOSS_PCT, FORCE_CLOSE_DTE

    tracked = load_positions()
    raw_legs: list[dict] = []

    # ── Locally tracked positions (full metadata available) ──────────────────
    for option_symbol, data in tracked.items():
        underlying  = data.get("underlying", "")
        entry_price = float(data.get("entry_price", 0))
        quantity    = int(data.get("quantity", 1))
        expiration  = data.get("expiration", "")
        earnings_date = data.get("earnings_date")

        current_price = round(get_option_mid(option_symbol, underlying, expiration), 2)
        if current_price <= 0:
            current_price = entry_price

        pnl_pct    = round((current_price - entry_price) / entry_price * 100, 1) if entry_price > 0 else 0
        pnl_dollar = round((current_price - entry_price) * quantity * 100, 2)
        dte        = days_until_expiry(expiration)

        if pnl_pct / 100 >= TAKE_PROFIT_PCT * 0.85:
            exit_trigger = f"TP near (+{round(TAKE_PROFIT_PCT*100)}%)"
        elif pnl_pct / 100 <= STOP_LOSS_PCT * 0.85:
            exit_trigger = f"SL near ({round(STOP_LOSS_PCT*100)}%)"
        elif dte <= FORCE_CLOSE_DTE + 2:
            exit_trigger = f"Expiry in {dte}d"
        elif earnings_date:
            try:
                edate = datetime.datetime.strptime(earnings_date, "%Y-%m-%d").date()
                # ET-anchored "days until earnings" — matches how the trade's
                # earnings_date was computed at entry.
                days_to_earn = (edate - today_est()).days
                exit_trigger = f"Earnings in {days_to_earn}d" if days_to_earn >= 0 else "Post-earnings close"
            except Exception:
                exit_trigger = "Watching"
        else:
            exit_trigger = "Watching"

        try:
            exp_display = datetime.datetime.strptime(expiration, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            exp_display = expiration

        parsed = _parse_occ_symbol(option_symbol)

        raw_legs.append({
            "symbol":        option_symbol,
            "underlying":    underlying,
            "expiration":    expiration,
            "exp_display":   exp_display,
            "dte":           dte,
            "quantity":      quantity,
            "entry_price":   entry_price,
            "current_price": current_price,
            "pnl_pct":       pnl_pct,
            "pnl_dollar":    pnl_dollar,
            "exit_trigger":  exit_trigger,
            "earnings_date": earnings_date,
            "source":        "local",
            "opt_type":      parsed["type"] if parsed else "?",
            "opt_type_label": "Call" if (parsed and parsed["type"] == "C") else "Put" if (parsed and parsed["type"] == "P") else "?",
            "strike":        parsed["strike"] if parsed else 0,
        })

    # ── Broker positions not in the local file (unsynced / manual trades) ────
    try:
        broker_positions = get_open_positions()
        tracked_symbols  = set(tracked.keys())

        for pos in broker_positions:
            symbol   = pos.get("symbol", "")
            quantity = float(pos.get("quantity", 0))

            if symbol in tracked_symbols or len(symbol) <= 6:
                continue  # already shown, or not an option

            parsed = _parse_occ_symbol(symbol)
            if not parsed:
                continue

            underlying  = parsed["underlying"]
            expiration  = parsed["expiry"]
            cost_basis  = float(pos.get("cost_basis", 0) or 0)
            entry_price = round(abs(cost_basis) / (abs(quantity) * 100), 2) if quantity != 0 else 0

            current_price = round(get_option_mid(symbol, underlying, expiration), 2)
            if current_price <= 0:
                current_price = entry_price

            dte = days_until_expiry(expiration)
            try:
                exp_display = datetime.datetime.strptime(expiration, "%Y-%m-%d").strftime("%m/%d")
            except Exception:
                exp_display = expiration

            if entry_price > 0 and quantity > 0:
                pnl_pct    = round((current_price - entry_price) / entry_price * 100, 1)
                pnl_dollar = round((current_price - entry_price) * quantity * 100, 2)
            elif quantity < 0:
                pnl_pct    = round((entry_price - current_price) / entry_price * 100, 1) if entry_price > 0 else 0
                pnl_dollar = round((entry_price - current_price) * abs(quantity) * 100, 2)
            else:
                pnl_pct = pnl_dollar = 0

            raw_legs.append({
                "symbol":        symbol,
                "underlying":    underlying,
                "expiration":    expiration,
                "exp_display":   exp_display,
                "dte":           dte,
                "quantity":      int(quantity),
                "entry_price":   entry_price,
                "current_price": current_price,
                "pnl_pct":       pnl_pct,
                "pnl_dollar":    pnl_dollar,
                "exit_trigger":  "Watching",
                "earnings_date": None,
                "source":        "broker",
                "opt_type":      parsed["type"],
                "opt_type_label": "Call" if parsed["type"] == "C" else "Put",
                "strike":        parsed["strike"],
            })
    except Exception as e:
        print(f"[Dashboard] Could not fetch broker positions for display: {e}")

    # ── Group legs into logical positions ────────────────────────────────────
    groups: dict[tuple, list[dict]] = {}
    for leg in raw_legs:
        key = (leg["underlying"], leg["expiration"])
        groups.setdefault(key, []).append(leg)

    results = []
    for (underlying, expiration), legs in groups.items():
        pos_type = _classify_position_type(legs)
        is_spread = len(legs) > 1

        # Aggregate P&L across all legs in the position
        total_pnl_dollar = round(sum(l["pnl_dollar"] for l in legs), 2)
        # Net cost basis for P&L % (sum of debit legs minus credit legs)
        total_cost = sum(abs(l["entry_price"] * l["quantity"] * 100) for l in legs)
        net_pnl_pct = round(total_pnl_dollar / total_cost * 100, 1) if total_cost > 0 else 0

        # For display: use first leg's meta for shared fields
        first = legs[0]

        # Build a compact contract description
        if is_spread:
            # Sort legs by strike for clean display
            sorted_legs = sorted(legs, key=lambda l: l["strike"])
            contract_parts = []
            for l in sorted_legs:
                side = "+" if l["quantity"] > 0 else "-"
                contract_parts.append(f"{side}{abs(l['quantity'])} ${l['strike']:.0f}{l['opt_type']}")
            contract_desc = " / ".join(contract_parts)
        else:
            leg = legs[0]
            side = "+" if leg["quantity"] > 0 else "-"
            contract_desc = f"{side}{abs(leg['quantity'])} ${leg['strike']:.0f}{leg['opt_type']}"

        results.append({
            "underlying":      underlying,
            "expiration":      expiration,
            "exp_display":     first["exp_display"],
            "dte":             first["dte"],
            "position_type":   pos_type,
            "is_spread":       is_spread,
            "contract_desc":   contract_desc,
            "legs":            [{
                "symbol":        l["symbol"],
                "quantity":      l["quantity"],
                "strike":        l["strike"],
                "opt_type":      l["opt_type"],
                "entry_price":   l["entry_price"],
                "current_price": l["current_price"],
                "pnl_pct":       l["pnl_pct"],
                "pnl_dollar":    l["pnl_dollar"],
            } for l in sorted(legs, key=lambda l: l["strike"])],
            "total_pnl_dollar": total_pnl_dollar,
            "total_pnl_pct":   net_pnl_pct,
            "exit_trigger":    first["exit_trigger"],
            "earnings_date":   first.get("earnings_date"),
            "source":          first["source"],
        })

    position_count = len(results)
    return results, position_count


def build_broker_trade_history() -> list[dict]:
    """
    Build trade history from Tradier's gain/loss API when the local
    journal (closed_trades.json) is empty.

    Returns records shaped like trade_journal output so the dashboard
    JS can render them with the same code.
    """
    try:
        raw = get_gainloss()
    except Exception as e:
        print(f"[Dashboard] Could not fetch broker gain/loss: {e}")
        return []

    results = []
    for gl in raw:
        symbol      = gl.get("symbol", "")
        quantity    = abs(float(gl.get("quantity", 0) or 0))
        cost        = float(gl.get("cost", 0) or 0)
        proceeds    = float(gl.get("proceeds", 0) or 0)
        pnl_dollar  = float(gl.get("gain_loss", 0) or 0)
        open_date   = gl.get("open_date", "")
        close_date  = gl.get("close_date", "")

        # Per-share prices (options multiply by 100)
        entry_price = round(abs(cost) / (quantity * 100), 4) if quantity > 0 else 0
        exit_price  = round(abs(proceeds) / (quantity * 100), 4) if quantity > 0 else 0
        pnl_pct     = round((exit_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0

        # Hold duration
        hold_hours = None
        if open_date and close_date:
            try:
                dt_open  = datetime.datetime.fromisoformat(open_date)
                dt_close = datetime.datetime.fromisoformat(close_date)
                hold_hours = round((dt_close - dt_open).total_seconds() / 3600, 2)
            except Exception:
                pass

        parsed = _parse_occ_symbol(symbol)
        underlying = parsed["underlying"] if parsed else symbol[:6]

        results.append({
            "option_symbol":  symbol,
            "underlying":     underlying,
            "strategy":       "broker",
            "entry_price":    entry_price,
            "exit_price":     exit_price,
            "quantity":       int(quantity),
            "expiration":     parsed["expiry"] if parsed else "",
            "entry_time":     open_date,
            "exit_time":      close_date,
            "hold_hours":     hold_hours,
            "pnl_dollar":     round(pnl_dollar, 2),
            "pnl_pct":        pnl_pct,
            "exit_reason":    "broker_close",
            "earnings_date":  None,
            "setup_score":    None,
            "signals":        {},
        })

    # Sort by close date, newest last
    results.sort(key=lambda r: r.get("exit_time") or "")
    return results


def _merged_trade_history() -> list[dict]:
    """
    Combine the local trade journal with broker gain/loss history.

    Local records take precedence (they have richer metadata — strategy,
    score, signals). Broker records fill in trades that were opened/closed
    outside the bot or before the journal was wired up.

    De-duplicates by option_symbol + close_date so the same trade doesn't
    appear twice.
    """
    local  = load_closed_trades()
    broker = build_broker_trade_history()

    if not broker:
        return local[-100:]

    # Build a set of (option_symbol, close_date) from local records for dedup
    local_keys = set()
    for t in local:
        sym  = t.get("option_symbol", "")
        date = (t.get("exit_time") or "")[:10]  # YYYY-MM-DD
        if sym and date:
            local_keys.add((sym, date))

    # Only add broker records that aren't already in the local journal
    merged = list(local)
    for bt in broker:
        sym  = bt.get("option_symbol", "")
        date = (bt.get("exit_time") or "")[:10]
        if (sym, date) not in local_keys:
            merged.append(bt)

    # Sort by exit_time, return last 100
    merged.sort(key=lambda r: r.get("exit_time") or "")
    return merged[-100:]


def build_trades_data() -> list[dict]:
    """Build today's trade log from Tradier orders."""
    orders = get_orders_today()
    results = []

    for order in orders:
        status = order.get("status", "")
        side   = order.get("side", "")
        sym    = order.get("option_symbol", order.get("symbol", ""))
        price  = order.get("avg_fill_price", "—")
        qty    = order.get("quantity", 1)

        try:
            raw_time = order.get("transaction_date", "")
            time_str = datetime.datetime.fromisoformat(raw_time).strftime("%I:%M %p")
        except Exception:
            time_str = "—"

        underlying = sym[:4] if len(sym) > 6 else sym

        results.append({
            "ticker":  underlying,
            "action":  side.replace("_", " ").title() if side else "Unknown",
            "time":    time_str,
            "result":  "filled" if status == "filled" else ("risk" if status == "rejected" else status),
            "price":   price,
            "qty":     qty,
        })

    return results


def build_pnl_history() -> list[dict]:
    """
    Load rolling P&L history from persistent log.
    Appends today's P&L, keeps last 10 trading days.

    "Today" is the US/Eastern trading day so the rolling window matches the
    market's calendar, not the host's local one.
    """
    history_file = "pnl_history.json"
    today = today_est().isoformat()

    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                history = json.load(f)
        except Exception:
            history = []

    # Update or append today
    risk_state = load_state()
    today_pnl  = round(risk_state.get("realized_pnl", 0.0), 2)

    existing = [h for h in history if h["date"] != today]
    existing.append({"date": today, "pnl": today_pnl})

    # Keep last 10 days
    history = sorted(existing, key=lambda x: x["date"])[-10:]

    atomic_write_json(history_file, history)

    return history


def compute_pnl_summary(closed_history: list[dict], open_positions: list[dict],
                        start_equity: float | None = None) -> dict:
    """
    Cumulative profit-and-loss across the whole account, since inception.

    realized   — sum of every closed trade's P&L (the locked-in result).
    unrealized — current mark-to-market on all open positions (uses the grouped
                 per-position total, falling back to a flat single-leg P&L).
    total      — realized + unrealized = the complete economic P&L.
    *_pct      — each figure as a % return on `start_equity` (the first equity
                 snapshot = the account's starting capital). None when unknown.

    Pure (no I/O) so it is cheap to unit-test. `closed_history` is any list of
    closed-trade dicts carrying `pnl_dollar`; `open_positions` is the grouped
    positions list from build_positions_data().
    """
    realized = sum(float(t.get("pnl_dollar", 0) or 0) for t in closed_history)
    unrealized = sum(
        float(p.get("total_pnl_dollar", p.get("pnl_dollar", 0)) or 0)
        for p in open_positions
    )
    total = realized + unrealized
    summary = {
        "realized":     round(realized, 2),
        "unrealized":   round(unrealized, 2),
        "total":        round(total, 2),
        "start_equity": round(float(start_equity), 2) if start_equity else None,
        "realized_pct":   None,
        "unrealized_pct": None,
        "total_pct":      None,
    }
    se = float(start_equity) if start_equity else 0.0
    if se > 0:
        summary["realized_pct"]   = round(realized / se * 100, 2)
        summary["unrealized_pct"] = round(unrealized / se * 100, 2)
        summary["total_pct"]      = round(total / se * 100, 2)
    return summary


# ─── Strategy panel, market regime, and events feed ───────────────────────────

# (strategy tag, heartbeat key, display name, position-book file, book-is-list)
_STRATEGY_META = [
    ("catalyst_long_call", "executor",       "Catalyst", "open_positions.json",     False),
    ("iv_rank",            "iv_rank_bot",     "IV-Rank",  "iv_rank_positions.json",  True),
    ("hft_intraday",       "hft_executor",    "HFT",      "hft_positions.json",      True),
    ("pead",               "pead_executor",   "PEAD",     "pead_positions.json",     True),
    ("bounce",             "bounce_executor", "Bounce",   "bounce_positions.json",   True),
]
HEARTBEAT_STALE_SEC = 600   # a strategy silent this long is treated as down

def _market_regime() -> dict:
    """Bull/bear regime for the dashboard. Delegates to the shared, per-day-cached
    market_regime module so there's no extra SPY fetch."""
    from mawitek.data.market_regime import current_regime
    r = current_regime()
    return {"state": r["state"], "detail": r["detail"]}


def _leg_symbols(record: dict) -> list[str]:
    """Every option symbol inside a (possibly multi-leg) position record."""
    out = []
    for leg in (record.get("legs") or []):
        s = leg.get("option_symbol") or leg.get("symbol")
        if s:
            out.append(s)
    return out


def _symbol_strategy_map() -> dict[str, str]:
    """
    Map each held option symbol → the strategy that opened it.

    The broker doesn't tell us which strategy owns a position, but each
    strategy's own position book does. We key by the book's canonical strategy
    (from _STRATEGY_META), NOT the per-record "strategy" field — multi-leg books
    (iv_rank) tag records with the structure name (e.g. "bull_put_spread"), which
    isn't the strategy the dashboard groups by.
    """
    mapping: dict[str, str] = {}
    for strat_key, _hb, _name, book, _is_list in _STRATEGY_META:
        try:
            if not os.path.exists(book):
                continue
            with open(book, "r") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            # catalyst book: keyed BY the option symbol; value holds the metadata.
            for sym, rec in data.items():
                mapping[sym] = strat_key
                if isinstance(rec, dict):
                    for s in _leg_symbols(rec):
                        mapping[s] = strat_key
        elif isinstance(data, list):
            for rec in data:
                if not isinstance(rec, dict):
                    continue
                s0 = rec.get("option_symbol") or rec.get("symbol")
                if s0:
                    mapping[s0] = strat_key
                for s in _leg_symbols(rec):
                    mapping[s] = strat_key
    return mapping


def _strategy_for_position(pos: dict, sym_map: dict[str, str]) -> str:
    """The strategy that owns a grouped position: the first leg symbol that maps to
    a strategy book wins; broker-only positions are 'unattributed'."""
    for leg in pos.get("legs", []):
        strat = sym_map.get(leg.get("symbol"))
        if strat:
            return strat
    top = pos.get("option_symbol") or pos.get("symbol")
    return sym_map.get(top, "unattributed")


def tag_positions_with_strategy(positions: list[dict]) -> list[dict]:
    """
    Annotate each open position with the strategy that opened it (matched by option
    symbol via the per-strategy books — the broker doesn't tell us). Tags in place
    and returns the list, so the dashboard can show a strategy badge and filter
    positions per strategy. Builds the symbol map once.
    """
    sym_map = _symbol_strategy_map()
    for pos in positions:
        pos["strategy"] = _strategy_for_position(pos, sym_map)
    return positions


def unrealized_by_strategy(positions: list[dict]) -> dict[str, float]:
    """
    Sum each open position's unrealized P&L by its attached strategy tag (set by
    tag_positions_with_strategy). Positions with no local-book match fall into the
    'unattributed' bucket so the per-strategy split reconciles with the portfolio
    total. Uses the grouped per-position P&L, falling back to summing the legs.
    """
    out: dict[str, float] = {}
    for pos in positions:
        strat = pos.get("strategy") or "unattributed"
        val = pos.get("total_pnl_dollar")
        if val is None:
            val = sum(float(l.get("pnl_dollar", 0) or 0) for l in pos.get("legs", []))
        out[strat] = out.get(strat, 0.0) + float(val or 0)
    return {k: round(v, 2) for k, v in out.items()}


def build_strategy_panel(equity: float, closed_trades: list[dict],
                         positions: list[dict] | None = None) -> dict:
    """
    Per-strategy operational view: live heartbeat health, positions + capital
    deployed vs allocation, realized AND unrealized P&L, portfolio concentration
    by correlation cluster, and the market regime.
    """
    from mawitek.infra.heartbeat import read_heartbeats
    from mawitek.core.risk_manager import (
        STRATEGY_ALLOCATION_PCT, deployed_capital_by_strategy, _count_book,
        MAX_POSITIONS_PER_GROUP, correlation_group, _open_underlyings,
    )

    hbs       = read_heartbeats()
    deployed  = deployed_capital_by_strategy()
    now       = time.time()
    positions = positions or []
    if positions and not positions[0].get("strategy"):    # ensure attribution for direct callers
        tag_positions_with_strategy(positions)
    unreal    = unrealized_by_strategy(positions)         # open P&L per strategy

    # Realized stats per strategy from the closed-trade journal.
    stats: dict[str, dict] = {}
    for t in closed_trades:
        s = stats.setdefault(t.get("strategy", "unknown"), {"trades": 0, "wins": 0, "pnl": 0.0})
        p = float(t.get("pnl_dollar", 0) or 0)
        s["trades"] += 1
        s["pnl"]    += p
        if p > 0:
            s["wins"] += 1

    health, strategies = [], []
    for key, hb, name, book, is_list in _STRATEGY_META:
        rec = hbs.get(hb)
        if rec:
            age = now - float(rec.get("ts", 0) or 0)
            health.append({"key": key, "name": name, "status": rec.get("status", "?"),
                           "age_sec": round(age), "alive": age <= HEARTBEAT_STALE_SEC,
                           "iso": rec.get("iso", "")})
        else:
            health.append({"key": key, "name": name, "status": "offline",
                           "age_sec": None, "alive": False, "iso": ""})

        cap = equity * STRATEGY_ALLOCATION_PCT.get(key, 0)
        dep = float(deployed.get(key, 0.0))
        st  = stats.get(key, {"trades": 0, "wins": 0, "pnl": 0.0})
        strategies.append({
            "key": key, "name": name,
            "positions": _count_book(book, is_list=is_list),
            "deployed":  round(dep, 2),
            "cap":       round(cap, 2),
            "alloc_pct": round(STRATEGY_ALLOCATION_PCT.get(key, 0) * 100),
            "usage_pct": round(dep / cap * 100) if cap > 0 else 0,
            "trades":    st["trades"],
            "win_rate":  round(st["wins"] / st["trades"] * 100) if st["trades"] else 0,
            "pnl":        round(st["pnl"], 2),     # realized
            "unrealized": unreal.get(key, 0.0),    # open mark-to-market
        })

    # Concentration: how full each correlation cluster is (portfolio-wide).
    counts: dict[str, int] = {}
    for u in _open_underlyings():
        g = correlation_group(u)
        if g:
            counts[g] = counts.get(g, 0) + 1
    concentration = [
        {"group": g.replace("_", " "), "count": c, "max": MAX_POSITIONS_PER_GROUP}
        for g, c in sorted(counts.items(), key=lambda x: -x[1])
    ]

    return {"health": health, "strategies": strategies,
            "concentration": concentration, "regime": _market_regime(),
            "unrealized_unattributed": unreal.get("unattributed", 0.0)}


def build_events(limit: int = 30) -> list[dict]:
    """Recent events (fills / closes / halts / big moves), newest first."""
    data = read_json("events.json", [])
    if not isinstance(data, list):
        return []
    return data[-limit:][::-1]


def build_news(limit: int = 40) -> list[dict]:
    """Recent categorized headlines from the news monitor (news_feed.json),
    newest first. The News tab also fetches news_feed.json directly for a
    faster refresh; this embedded slice is the first-paint / fallback copy."""
    data = read_json("news_feed.json", [])
    if not isinstance(data, list):
        return []
    return data[:limit]   # the monitor already stores newest-first


def build_social(limit: int = 40) -> list[dict]:
    """Per-ticker social sentiment (social_sentiment.json), most-discussed first.
    Like build_news, this is the first-paint / fallback copy — the Social tab
    fetches social_sentiment.json directly for a faster refresh."""
    data = read_json("social_sentiment.json", [])
    if not isinstance(data, list):
        return []
    return data[:limit]   # the social sweep already stores volume-sorted


def _enrich_setups_with_iv(setups: list[dict] | None, max_fetch: int = 12) -> list[dict]:
    """
    Attach `iv` context (cheap/rich vol regime) to setups. Day-cached inside
    iv_provider, and capped at `max_fetch` NEW broker reads per cycle so a big
    board can't stall the dashboard. Setups already carrying `iv` are skipped
    (it's a daily-stable metric). Best-effort — never raises.
    """
    if not setups:
        return setups or []
    try:
        from mawitek.data.iv_provider import iv_context
    except Exception:
        return setups
    fetched = 0
    for s in setups:
        if s.get("iv") or fetched >= max_fetch:
            continue
        ticker = s.get("ticker")
        if not ticker:
            continue
        try:
            ctx = iv_context(ticker)
        except Exception:
            ctx = None
        if ctx:
            s["iv"] = ctx
            fetched += 1
    return setups


def _alert_channel_status() -> dict:
    """Which notification channels are configured (no tokens exposed). Best-effort."""
    try:
        from mawitek.infra.event_notifier import channel_status
        return channel_status()
    except Exception:
        return {}


def _safe_drawdown_status(equity: float) -> dict | None:
    """drawdown_status() but never lets a hiccup break the whole state write."""
    try:
        return drawdown_status(equity)
    except Exception as e:
        print(f"[Dashboard] drawdown_status failed (non-fatal): {e}")
        return None


def write_dashboard_state(
    setups: list[dict] | None = None,
    bot_status: str = "running",
    account_mode: str = "paper",
):
    """
    Write full dashboard state to JSON.
    Call this at the end of every executor scan cycle.

    Args:
        setups:       List of scanner setup dicts from options_scanner
        bot_status:   "running", "halted", "scanning", "idle"
        account_mode: "paper" or "live"
    """
    try:
        balances   = get_account_balance()
        equity     = balances.get("total_equity", 0)
        risk_state = load_state()

        # Account-size tier config (tiers + dashboard overrides) — drives the
        # account caps below and feeds the dashboard's Settings form. Best-effort:
        # a config error must never stop the state write, so fall back to None.
        try:
            from mawitek.core.user_config import (effective_config, load_user_config,
                                     alert_config, TIER_PRESETS, TIER_THRESHOLDS)
            cfg = effective_config(equity)
        except Exception as e:
            print(f"[Dashboard] Could not resolve tier config (non-fatal): {e}")
            cfg = None

        daily_pnl     = round(risk_state.get("realized_pnl", 0.0), 2)
        trades_today  = risk_state.get("trades_today", 0)
        halted        = risk_state.get("halted", False)
        loss_limit    = round(equity * DAILY_LOSS_LIMIT_PCT, 2)
        loss_used_pct = round(abs(daily_pnl) / loss_limit * 100, 1) if loss_limit > 0 and daily_pnl < 0 else 0

        positions, open_count = build_positions_data()
        tag_positions_with_strategy(positions)   # attach pos["strategy"] for per-strategy views
        trades        = build_trades_data()
        pnl_history   = build_pnl_history()

        # Derive swing/day split from the positions already fetched so the
        # sub-totals always add up to open_count (no mismatch with broker-only
        # positions that count_positions_by_type wouldn't see).
        swing_count = sum(1 for p in positions if p["dte"] > DAY_TRADE_MAX_DTE)
        day_count   = sum(1 for p in positions if p["dte"] <= DAY_TRADE_MAX_DTE)

        # Keep the last real scan's setups visible instead of blanking the card
        # every idle/scanning cycle. Saves fresh setups; restores them when empty.
        setups, setups_updated = _persist_or_restore_setups(setups)

        # Attach IV context (cheap/rich vol regime) to setups — informational, so
        # you can see whether each name's options are expensive. Day-cached, with a
        # bounded number of new broker reads per cycle.
        setups = _enrich_setups_with_iv(setups)

        # Track each surfaced setup's forward return (did the scanner pick movers?)
        # and aggregate the scanner's hit-rate-by-score. Best-effort + never blocks.
        try:
            from mawitek.analysis.setup_tracker import track_and_persist, scanner_performance
            setups = track_and_persist(setups)
            scanner_perf = scanner_performance(setups)
        except Exception as e:
            print(f"[Dashboard] Scanner-performance tracking failed (non-fatal): {e}")
            scanner_perf = {}

        # Filter pass rate stats
        filter_stats = {"earnings": 0, "flow": 0, "news": 0, "momentum": 0, "total": 0}
        if setups:
            filter_stats["total"] = len(setups)
            for s in setups:
                if s.get("days_until_earnings") is not None: filter_stats["earnings"] += 1
                if s.get("options_flow"):                    filter_stats["flow"] += 1
                if s.get("news_catalyst"):                   filter_stats["news"] += 1
                if s.get("momentum_score", 0) >= 40:        filter_stats["momentum"] += 1

        def pct(n, total):
            return round(n / total * 100) if total > 0 else 0

        closed_trades  = load_closed_trades()
        strategy_panel = build_strategy_panel(equity, closed_trades, positions)

        # All-time P&L for the Overview headline: realized = every closed trade in
        # the journal (the same authoritative, uncapped source the Strategies tab
        # and Analytics metrics use, so the figures reconcile) + unrealized =
        # current mark-to-market on open positions, plus each figure as a % return
        # on the account's starting capital (the first equity-curve snapshot).
        equity_curve = load_equity_curve()
        start_equity = float(equity_curve[0].get("equity", 0) or 0) if equity_curve else 0.0
        pnl_summary  = compute_pnl_summary(closed_trades, positions, start_equity)

        # Net option greeks across the whole book (delta/gamma/theta/vega).
        # Also caches net vega for the risk manager's portfolio-vega limit.
        try:
            from mawitek.core.portfolio_greeks import compute_portfolio_greeks
            greeks = compute_portfolio_greeks(positions)
            greeks["vega_limit"] = round(equity * VEGA_LIMIT_PCT, 2)
        except Exception as e:
            print(f"[Dashboard] Could not compute portfolio greeks: {e}")
            greeks = {}

        state = {
            # ET-anchored — the dashboard renders this timestamp next to a
            # "last update" indicator, and stale-bar math expects ET.
            "timestamp":    now_est().isoformat(),
            "account_mode": account_mode,
            "bot_status":   "halted" if halted else bot_status,
            # How often the bot is expected to rewrite this file (≈ the catalyst
            # scan cadence: 5 min while open, 30 min after hours). The dashboard
            # uses this so a normal slow-cadence write isn't flagged as "stale".
            "update_interval_sec": 1800 if bot_status in ("scanning_closed", "idle") else 300,
            "account": {
                "equity":          round(equity, 2),
                "daily_pnl":       daily_pnl,
                "daily_pnl_pct":   round(daily_pnl / equity * 100, 2) if equity > 0 else 0,
                "loss_limit":      loss_limit,
                "loss_used":       abs(daily_pnl) if daily_pnl < 0 else 0,
                "loss_used_pct":   loss_used_pct,
                "open_positions":  open_count,
                "max_positions":   (cfg["max_swing_positions"] + cfg["max_day_positions"]) if cfg else MAX_OPEN_POSITIONS,
                "swing_positions": swing_count,
                "max_swing":       cfg["max_swing_positions"] if cfg else MAX_SWING_POSITIONS,
                "day_positions":   day_count,
                "max_day":         cfg["max_day_positions"] if cfg else MAX_DAY_POSITIONS,
                "trades_today":    trades_today,
                "halted":          halted,
                # Drawdown governor: peak-to-current drawdown, rolling week, and
                # whether new entries are being de-risked or halted to protect
                # profits from a slow bleed the daily halt misses. Best-effort.
                "drawdown":        _safe_drawdown_status(equity),
            },
            # Active tier config + presets + thresholds for the Settings form.
            "config": ({
                "effective":  cfg,
                "raw":        load_user_config(),
                "tiers":      TIER_PRESETS,
                "thresholds": TIER_THRESHOLDS,
                "alerts":     alert_config(),
            } if cfg else {}),
            "pnl_summary": pnl_summary,
            "positions":   positions,
            "trades":      trades,
            "pnl_history": pnl_history,
            "setups":      setups or [],
            "setups_updated": setups_updated,
            "scanner_perf": scanner_perf,
            "filter_stats": {
                "earnings_pct":  pct(filter_stats["earnings"],  filter_stats["total"]),
                "flow_pct":      pct(filter_stats["flow"],      filter_stats["total"]),
                "news_pct":      pct(filter_stats["news"],      filter_stats["total"]),
                "momentum_pct":  pct(filter_stats["momentum"],  filter_stats["total"]),
            },
            "trade_history": _merged_trade_history(),
            "decision_log":  load_recent_decisions(limit=200),
            "equity_curve":  equity_curve[-500:],
            "metrics":       compute_metrics(equity_curve, closed_trades),
            "strategy_panel": strategy_panel,
            "greeks":         greeks,
            "events":         build_events(),
            "alerts":         _alert_channel_status(),
            "news":           build_news(),
            "social":         build_social(),
        }

        # Atomic write so the dashboard (and any other process) never reads a
        # half-written state file.
        atomic_write_json(STATE_FILE, state)

        print(f"[Dashboard] State written -> {STATE_FILE}")

    except Exception as e:
        print(f"[Dashboard] Error writing state: {e}")
