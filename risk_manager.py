"""
risk_manager.py — central risk controls shared by all five strategies.

Every entry path runs through pre_trade_check(), which enforces:

    1. Account equity sanity                                     (>0)
    2. Daily-loss halt                                           (DAILY_LOSS_LIMIT_PCT)
    3. Bear-regime throttle: shrink budget + slot caps           (BEAR_REGIME_THROTTLE)
    4. Per-trade-type position cap: swing vs day, independent    (MAX_SWING_POSITIONS / MAX_DAY_POSITIONS)
    5. Duplicate-position guard                                  (one option per underlying)
    6. Correlation-cluster cap                                   (MAX_POSITIONS_PER_GROUP)
    7. Per-trade position size                                   (RISK_PER_TRADE_PCT, capped at MAX_POSITION_SIZE_PCT)
    8. Per-strategy capital allocation                           (STRATEGY_ALLOCATION_PCT)

State is persisted to risk_state.json (daily P&L, halt flag) and
halt_events.json (audit log of every halt). All writes go through
state_io's atomic_write_json + cross-process file_lock so the five
strategy processes never corrupt or lose-update each other's writes.

Day vs swing classification:
    classify_trade_type(strategy, dte) — a ≤DAY_TRADE_MAX_DTE contract is
    always a DAY trade regardless of strategy; everything else maps via
    SWING_STRATEGIES / DAY_STRATEGIES.

Daily P&L (calculate_daily_pnl):
    True mark-to-market: current live equity minus the most recent equity
    snapshot from before today (equity_tracker). Falls back to realized P&L
    only on day-one when no prior snapshot exists. This is the right number;
    an older version subtracted every buy and added every sell from the day's
    order tape, which booked every opening debit as a loss and could false-
    trip the halt mid-session.
"""

import json
import os
from tradier_client import get_account_balance, get_open_positions
from utils import now_est, today_est
from state_io import atomic_write_json, file_lock


# ─── Risk Config ───────────────────────────────────────────────────────────────

RISK_PER_TRADE_PCT    = 0.03    # Risk 3% of account per trade
DAILY_LOSS_LIMIT_PCT  = 0.05    # Halt if down 5% on the day
MAX_POSITION_SIZE_PCT = 0.05    # No single position > 5% of account

# ── Bear-market risk throttle ───────────────────────────────────────────────────
# When the broad market is in a confirmed downtrend (SPY < 200-day SMA), the
# bot's long-biased edge weakens, so every NEW trade is automatically de-risked:
# a smaller budget, fewer concurrent slots, and (optionally) no new long-
# directional entries at all. The regime comes from market_regime, which caches
# it per ET day — so this adds NO per-trade network cost (one SPY fetch/day for
# the whole process). Set BEAR_REGIME_THROTTLE=False to disable entirely.
BEAR_REGIME_THROTTLE = True     # master switch
BEAR_SIZE_MULT       = 0.5      # bear regime: halve the per-trade budget
BEAR_POSITION_MULT   = 0.6      # bear regime: ~40% fewer slots (swing 7->4, day 5->3)
BEAR_PAUSE_LONGS     = False    # if True, refuse NEW long-directional entries outright
# Strategies whose entries are outright long bets (the ones paused when
# BEAR_PAUSE_LONGS is on). Premium-selling (iv_rank) and intraday (hft) are only
# de-risked, never hard-paused — they can still work in a downtrend.
LONG_DIRECTIONAL_STRATEGIES = {"catalyst_long_call", "pead"}
# Strategies whose edge IMPROVES in a bear market — fully EXEMPT from the throttle
# so they trade at validated (full) size exactly when it matters. The capitulation
# bounce is the bot's bear-market offense; throttling it would defeat the purpose.
BEAR_THROTTLE_EXEMPT = {"bounce"}

# ── Drawdown governor (protect profits from a slow bleed) ───────────────────────
# The daily-loss halt (above) resets every midnight, so a grind of small daily
# losses walks straight through it — e.g. the account fell ~17% over a month with
# no single -5% day. The governor instead measures loss from the HIGH-WATER MARK
# (peak equity, ratcheted up and persisted) and over a rolling week, de-risking
# NEW entries gradually before halting them entirely. It never touches existing
# positions — those ride to their own TP/SL exits. Fails OPEN (a data hiccup
# never blocks trading). The high-water mark anchors at current equity on first
# run (re-baseline to "now"); delete drawdown_state.json to re-anchor later.
DRAWDOWN_GOVERNOR     = True     # master switch
DD_DERISK_HALF_PCT    = 0.06     # >= 6% off peak  → half-size new entries
DD_DERISK_QUARTER_PCT = 0.10     # >= 10% off peak → quarter-size new entries
DD_HALT_PCT           = 0.13     # >= 13% off peak → stop opening new positions
WEEKLY_LOSS_LIMIT_PCT = 0.08     # rolling N-day loss → stop opening new positions
WEEKLY_LOOKBACK_DAYS  = 5        # trading days in the rolling window
DRAWDOWN_STATE_FILE   = "drawdown_state.json"

# ── Swing vs Day position budgets ──────────────────────────────────────────────
# Swing and day trades get SEPARATE position caps so a full swing book (multi-day
# catalyst calls / IV-rank spreads) can never use up the slots the intraday
# day-trading strategy needs — and vice versa. Each is counted and capped on its
# own. Tune these independently.
MAX_SWING_POSITIONS = 8         # catalyst + iv_rank + pead + bounce (multi-day holds)
MAX_DAY_POSITIONS   = 5         # hft_intraday (0-DTE, flat by EOD)
MAX_OPEN_POSITIONS  = MAX_SWING_POSITIONS + MAX_DAY_POSITIONS   # combined (dashboard/back-compat)

# Which strategies count as which book.
SWING_STRATEGIES = {"catalyst_long_call", "iv_rank", "pead", "bounce"}
DAY_STRATEGIES   = {"hft_intraday"}

# A trade with this DTE or less is a DAY trade regardless of strategy — it
# expires (almost) today, so it's an intraday play, not an overnight hold.
DAY_TRADE_MAX_DTE = 1

# ── Correlation / concentration cap ────────────────────────────────────────────
# The position-count caps above treat every slot as independent, but 5 day-trade
# longs in SPY/QQQ/AAPL/MSFT/NVDA is NOT five independent bets — it's one
# leveraged wager on large-cap tech beta. This cap limits how many concurrent
# positions may sit in the same correlation cluster (across ALL strategies, since
# correlation risk is portfolio-wide). A ticker maps to the FIRST group that
# contains it; tickers in no group are unconstrained. Coarse but effective as a
# first-line guard — it is NOT a full covariance model. Keep the lists current.
MAX_POSITIONS_PER_GROUP = 3

CORRELATION_GROUPS: dict[str, set[str]] = {
    # The dominant index drivers — they move together and with SPY/QQQ.
    "megacap_growth": {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG",
                       "TSLA", "AVGO", "NFLX"},
    "index":          {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "MDY", "RSP"},
    "semis":          {"AMD", "MU", "INTC", "QCOM", "TXN", "AMAT", "KLAC", "LRCX",
                       "MRVL", "ARM", "SMCI", "ON", "MCHP", "ADI", "NXPI", "TSM", "ASML"},
    "software":       {"CRM", "ADBE", "ORCL", "NOW", "PLTR", "SNOW", "NET", "DDOG",
                       "ZS", "CRWD", "PANW", "SHOP", "MDB", "TEAM", "INTU", "ANET"},
    "crypto":         {"MSTR", "COIN", "HOOD", "MARA", "RIOT", "CLSK"},
    "financials":     {"JPM", "BAC", "GS", "WFC", "C", "MS", "SCHW", "AXP", "BLK",
                       "COF", "V", "MA", "PYPL", "SPGI"},
    "energy":         {"XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY",
                       "DVN", "FANG"},
    "healthcare":     {"LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "TMO", "ABT",
                       "DHR", "AMGN", "GILD", "MRNA", "BMY"},
    "consumer":       {"WMT", "COST", "HD", "NKE", "MCD", "SBUX", "LOW", "TGT",
                       "PG", "KO", "PEP", "MDLZ", "ABNB", "UBER"},
}


# ── Portfolio vega limit ────────────────────────────────────────────────────────
# An options book's biggest hidden risk is net VEGA — its P&L sensitivity to a
# shift in implied volatility. The position-count and concentration caps don't
# see it: a stack of long calls (catalyst/pead/bounce/hft) is heavily LONG vol
# and bleeds in an IV crush, while iv_rank premium-selling is SHORT vol and
# bleeds in an IV spike. This caps how lopsided the book may get.
#
# net_vega is $ P&L per +1 implied-vol POINT. The cap is a fraction of equity:
# at 1% of a $100k account, a +1 vol-point move can't swing the book more than
# ~$1,000. Once |net_vega| is past the cap, NEW trades that would push vol
# exposure further in the SAME direction are refused (you can still trade the
# other side, which de-risks). Coarse, portfolio-wide, and FAILS OPEN when
# greeks are unavailable (never blocks trading on missing data).
PORTFOLIO_VEGA_LIMIT  = True
MAX_PORTFOLIO_VEGA_PCT = 0.01   # |net vega| cap = 1% of equity per vol point

# Which side of vol each strategy adds when it opens a position.
LONG_VEGA_STRATEGIES  = {"catalyst_long_call", "pead", "bounce", "hft_intraday"}  # buy options
SHORT_VEGA_STRATEGIES = {"iv_rank"}                                              # sell premium

# ── IV-aware sizing ─────────────────────────────────────────────────────────────
# Buying long premium when implied vol is RICH means overpaying and eating an IV
# crush — the exact mechanism that made the catalyst long-call backtest negative.
# So the MULTI-DAY long-premium buyers trade SMALLER when a name's IV is rich
# (full size otherwise). This sizes-down, it does NOT block trades, so trade
# frequency is unchanged. iv_rank (Strategy 2) is excluded — it has its OWN IV-rank
# logic. HFT is excluded — it's an intraday gamma play, insensitive to the 30-DTE
# IV level. Fails OPEN when IV is unavailable. IV context comes from iv_provider
# (day-cached). Tuning the multipliers would benefit from a backtest.
IV_AWARE_SIZING     = True
IV_SIZED_STRATEGIES = {"catalyst_long_call", "pead", "bounce"}
IV_RICH_MULT        = 0.6    # rich IV → 60% size
IV_VERY_RICH_MULT   = 0.4    # extreme IV → 40% size
IV_VERY_RICH_RANK   = 85.0   # "very rich" if IV rank >= this ...
IV_VERY_RICH_RATIO  = 1.6    # ... or IV/HV ratio >= this


def classify_trade_type(strategy: str | None, dte: int | None = None) -> str:
    """
    Determine whether a trade is a DAY trade or a SWING trade from its actual
    structure, not just which strategy opened it.

        DAY   — opened and (intended to be) closed within the same session.
                0-1 DTE options, intraday triggers, flat by EOD.
        SWING — held overnight to days/weeks. 7-45 DTE, catalyst/technical
                setups managed by TP/SL/DTE over multiple days.

    Rule: a very-short-DTE contract (<= DAY_TRADE_MAX_DTE) is always a day
    trade — it can't be held meaningfully overnight. Otherwise we fall back to
    the strategy's intent (hft = day; catalyst / iv-rank = swing).
    """
    if dte is not None and dte <= DAY_TRADE_MAX_DTE:
        return "day"
    if strategy in DAY_STRATEGIES:
        return "day"
    return "swing"

# Per-strategy capital allocation — the max fraction of account equity each
# strategy may have DEPLOYED (sum of open-position cost basis) at once. Keeps
# one strategy from hogging buying power so the others can't trade. A strategy
# not listed here is uncapped (only the global checks apply). Total can exceed
# 1.0 since not every strategy is fully deployed simultaneously.
STRATEGY_ALLOCATION_PCT = {
    "catalyst_long_call": 0.40,   # swing catalyst book
    "iv_rank":            0.25,   # premium / spreads
    "hft_intraday":       0.20,   # intraday 0-DTE
    "pead":               0.15,   # post-earnings / news drift (swing)
    "bounce":             0.15,   # bear-regime capitulation bounce (only deploys in bear)
}

# State file to persist daily P&L across restarts
STATE_FILE      = "risk_state.json"
HALT_EVENTS_FILE = "halt_events.json"   # NEW: persistent log of every halt event


# ─── State Management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    # "Today" must be the US/Eastern trading day, not the host's local day.
    # Otherwise a server in UTC rolls over hours before the NYSE close and
    # zeros out an active halt or P&L.
    today = today_est().isoformat()
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
        # Reset if it's a new trading day
        if state.get("date") != today:
            return default
        return state
    except Exception:
        return default


def save_state(state: dict):
    # Atomic write so a concurrent reader in another strategy process never
    # sees a half-written file (a corrupt risk_state could disable the halt).
    atomic_write_json(STATE_FILE, state)


def _log_halt_event(equity: float, pnl: float, limit: float) -> None:
    """Append a halt event for the dashboard's Risk tab."""
    event = {
        "timestamp": now_est().isoformat(timespec="seconds"),
        "date":      today_est().isoformat(),
        "equity":    round(equity, 2),
        "pnl":       round(pnl, 2),
        "limit":     round(limit, 2),
        "pnl_pct":   round((pnl / equity * 100) if equity > 0 else 0, 2),
    }
    try:
        existing: list = []
        if os.path.exists(HALT_EVENTS_FILE):
            with open(HALT_EVENTS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    existing = data
        existing.append(event)
        with open(HALT_EVENTS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"[RiskManager] Could not persist halt event: {e}")


# ─── Core Risk Checks ──────────────────────────────────────────────────────────

def active_config(equity: float) -> dict | None:
    """
    The effective, account-size-aware risk config for this equity (tiers +
    user overrides from the dashboard). Returns None — so every caller falls
    back to the module-level constants below — whenever user_config is
    unavailable or errors. The "standard" tier equals these constants exactly,
    so a normal account behaves identically with or without this layer.
    """
    try:
        from user_config import effective_config
        return effective_config(equity)
    except Exception:
        return None


def get_position_size(equity: float) -> float:
    """
    Returns max $ to allocate per trade based on account equity.
    Caps at the per-position ceiling. Both percentages come from the active
    tier config (falling back to RISK_PER_TRADE_PCT / MAX_POSITION_SIZE_PCT).
    """
    cfg = active_config(equity)
    risk_pct = cfg["risk_per_trade_pct"]    if cfg else RISK_PER_TRADE_PCT
    max_pct  = cfg["max_position_size_pct"] if cfg else MAX_POSITION_SIZE_PCT
    risk_amount  = equity * risk_pct
    max_position = equity * max_pct
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


# ── Liquidity / order-size cap ──────────────────────────────────────────────────
# Budget-based sizing says how many contracts we can AFFORD; it says nothing about
# how many the MARKET can absorb. Sending an order that's a large fraction of an
# option's open interest / daily volume means walking the book — you don't get the
# quoted mid the backtests assume, and the edge bleeds into slippage. This is
# invisible at 1-3 contracts and dominant at scale, so it grows exactly as the
# account grows. The cap limits an order to a fraction of the contract's available
# liquidity. FAILS OPEN: with no OI/volume data (e.g. a 0-DTE on expiration day,
# when OI is stale/zero) it does not cap, so it never blocks a trade on missing
# data. One lot is ALWAYS allowed (a single contract fills even in a thin option) —
# the cap only restrains SIZE. These are market-structure limits, not account-size
# ones, so they live here as globals rather than in the per-account tier config.
LIQUIDITY_CAP_ENABLED    = True
MAX_PCT_OF_OPEN_INTEREST = 0.05    # an order may be at most 5% of open interest
MAX_PCT_OF_VOLUME        = 0.10    # ...or 10% of today's volume (whichever is bigger)
MAX_CONTRACTS_ABS        = 500     # absolute backstop, even for very liquid names


def _as_int(v) -> int:
    """Coerce a possibly-str/None quote field to int; 0 on anything unparseable."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def liquidity_cap(contract: dict | None) -> int | None:
    """
    Max contracts this option's liquidity can absorb near the quoted mid, or None
    when there's no liquidity data to judge by (→ no cap; fail open).

    Uses the LARGER of a %-of-open-interest and a %-of-volume allowance — whichever
    signal is present and bigger. (A freshly-listed weekly has ~0 OI but real
    volume; a 0-DTE on expiration day has stale OI but real volume — so neither
    signal alone is reliable, and the more-liquid reading wins.) Floored at 1 (a
    single lot always fills) and backstopped by MAX_CONTRACTS_ABS.
    """
    if not LIQUIDITY_CAP_ENABLED or not contract:
        return None
    oi  = _as_int(contract.get("open_interest"))
    vol = _as_int(contract.get("volume"))
    caps: list[int] = []
    if oi > 0:
        caps.append(int(oi * MAX_PCT_OF_OPEN_INTEREST))
    if vol > 0:
        caps.append(int(vol * MAX_PCT_OF_VOLUME))
    if not caps:
        return None                                    # no data → don't cap
    return max(1, min(max(caps), MAX_CONTRACTS_ABS))


def size_contracts(budget: float, mid_price: float, equity: float,
                   strategy: str | None = None, contract: dict | None = None) -> int:
    """
    Contracts to trade for a long-option entry — affordability, the small-account
    floor, AND market liquidity, in one place.

    1. Affordability: calculate_contracts() — budget // cost, rounded DOWN.
    2. Min-one floor: when the budget rounds to 0 but the active tier enables
       `min_one_contract`, round UP to ONE — PROVIDED one contract fits the
       per-position ceiling (max_position_size_pct × equity), so the override can
       never breach the per-trade risk cap (else 0, logged).
    3. Liquidity cap: never send more than `liquidity_cap(contract)` — a fraction
       of the contract's OI/volume — so a large order can't walk the book past the
       mid. One lot is always allowed; only larger orders are restrained.

    With min_one_contract OFF and no `contract` passed, this is identical to
    calculate_contracts — large accounts behave exactly as before.
    """
    qty = calculate_contracts(budget, mid_price)

    # 2. Min-one floor (small accounts).
    if qty < 1 and mid_price > 0 and equity > 0:
        cfg = active_config(equity)
        if cfg and cfg.get("min_one_contract"):
            cost = mid_price * 100
            ceiling = equity * cfg.get("max_position_size_pct", MAX_POSITION_SIZE_PCT)
            if cost <= ceiling:
                print(
                    f"[RiskManager] min-one-contract: budget ${budget:,.0f} < ${cost:,.0f}/contract, "
                    f"but 1 contract fits the {cfg.get('tier')} ceiling ${ceiling:,.0f} — taking 1"
                    + (f" ({strategy})" if strategy else "")
                )
                qty = 1
            else:
                print(
                    f"[RiskManager] {strategy or 'trade'} skipped: 1 contract ${cost:,.0f} exceeds "
                    f"per-position ceiling ${ceiling:,.0f} (account too small for this contract)"
                )

    # 3. Liquidity cap (matters as size grows). One lot is always allowed.
    if qty > 1:
        cap = liquidity_cap(contract)
        if cap is not None and qty > cap:
            print(
                f"[RiskManager] {strategy or 'trade'} size capped {qty}->{cap} by liquidity "
                f"(OI={_as_int(contract.get('open_interest'))}, vol={_as_int(contract.get('volume'))}, "
                f"<={MAX_PCT_OF_OPEN_INTEREST:.0%} OI / <={MAX_PCT_OF_VOLUME:.0%} vol)"
            )
            qty = cap

    return qty


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
    """
    Count current open LONG option positions.

    Short legs (quantity < 0) are spread hedges placed by the IV-rank bot
    and should not consume a position slot — only long legs do.
    """
    positions = get_open_positions()
    return sum(
        1 for p in positions
        if len(p.get("symbol", "")) > 6          # option symbol
        and float(p.get("quantity", 0)) > 0      # long only
    )


def trade_type_for(strategy: str | None) -> str:
    """Map a strategy name to its position book: 'day' or 'swing' (default)."""
    return "day" if strategy in DAY_STRATEGIES else "swing"


def _count_book(path: str, is_list: bool) -> int:
    """Count entries in a local position book (a multi-leg position = 1)."""
    try:
        if not os.path.exists(path):
            return 0
        with open(path, "r") as f:
            data = json.load(f)
        if is_list:
            return len(data) if isinstance(data, list) else 0
        return len(data) if isinstance(data, dict) else 0
    except Exception:
        return 0


def correlation_group(ticker: str | None) -> str | None:
    """Return the correlation cluster a ticker belongs to, or None if untracked."""
    if not ticker:
        return None
    t = ticker.upper()
    for group, members in CORRELATION_GROUPS.items():
        if t in members:
            return group
    return None


def _book_underlyings(path: str) -> list[str]:
    """Underlyings from one local position book. Handles both the dict-keyed
    catalyst book and the list-based hft/iv_rank books, and both the
    'underlying' (catalyst/hft) and 'ticker' (iv_rank) field names."""
    out: list[str] = []
    try:
        if not os.path.exists(path):
            return out
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            entries = data.values()
        elif isinstance(data, list):
            entries = data
        else:
            return out
        for e in entries:
            if not isinstance(e, dict):
                continue
            u = e.get("underlying") or e.get("ticker")
            if u:
                out.append(str(u).upper())
    except Exception:
        pass
    return out


def _open_underlyings() -> list[str]:
    """Every open position's underlying, across all strategy books."""
    return (_book_underlyings("open_positions.json")
            + _book_underlyings("hft_positions.json")
            + _book_underlyings("iv_rank_positions.json")
            + _book_underlyings("pead_positions.json")
            + _book_underlyings("bounce_positions.json"))


def concentration_reject(ticker: str) -> str | None:
    """Reject reason if opening `ticker` would exceed MAX_POSITIONS_PER_GROUP in
    its correlation cluster, else None. Tickers in no cluster are never capped."""
    group = correlation_group(ticker)
    if not group:
        return None
    count = sum(1 for u in _open_underlyings() if correlation_group(u) == group)
    if count >= MAX_POSITIONS_PER_GROUP:
        return (f"Correlation cap for '{group}' "
                f"({count}/{MAX_POSITIONS_PER_GROUP} correlated positions already open)")
    return None


def _vega_reject(strategy: str | None, equity: float) -> str | None:
    """
    Reject reason if the book's net vega is already past the cap AND this trade
    would push vol exposure further the same way, else None.

    Reads the cached net vega (computed each dashboard cycle) so this stays cheap
    in the pre-trade hot path. FAILS OPEN — returns None whenever greeks are
    unavailable, the limit is off, equity is unknown, or the strategy's vol side
    is unclassified — so a missing greek read never blocks a trade.
    """
    if not PORTFOLIO_VEGA_LIMIT or equity <= 0:
        return None
    try:
        from portfolio_greeks import cached_net_vega
        net_vega = cached_net_vega()
    except Exception:
        return None
    if net_vega is None:
        return None

    cap = equity * MAX_PORTFOLIO_VEGA_PCT
    # Long-vol book already over cap → block more long-vol (option-buying) trades.
    if net_vega > cap and strategy in LONG_VEGA_STRATEGIES:
        return (f"Portfolio vega +${net_vega:,.0f} over long-vol cap "
                f"(${cap:,.0f}) — no new long-volatility trades")
    # Short-vol book already over cap → block more short-vol (premium-selling) trades.
    if net_vega < -cap and strategy in SHORT_VEGA_STRATEGIES:
        return (f"Portfolio vega -${abs(net_vega):,.0f} over short-vol cap "
                f"(${cap:,.0f}) — no new short-volatility trades")
    return None


def count_positions_by_type(trade_type: str) -> int:
    """
    Count the bot's MANAGED positions of a given trade type from its own local
    books. A multi-leg position (spread / condor / straddle) counts as ONE.

        day   = hft_positions.json                 (intraday 0-DTE)
        swing = open_positions.json (catalyst)     +
                iv_rank_positions.json (iv-rank)    +
                pead_positions.json (drift)         (multi-day holds)

    This is what the swing/day caps are enforced against, so the two strategy
    families can fill their slots independently.
    """
    if trade_type == "day":
        return _count_book("hft_positions.json", is_list=True)
    return (_count_book("open_positions.json", is_list=False)
            + _count_book("iv_rank_positions.json", is_list=True)
            + _count_book("pead_positions.json", is_list=True)
            + _count_book("bounce_positions.json", is_list=True))


def calculate_daily_pnl() -> float:
    """
    True daily P&L = current mark-to-market equity − baseline equity for today.

    Baseline = the most recent equity snapshot from a date BEFORE today
    (i.e. yesterday's close, or whenever the bot last ran).

    If there's no prior snapshot (first day of operation) we return the
    realized portion only, so the halt logic remains conservative on
    day one.
    """
    # Lazy import: equity_tracker imports position_manager which can cause
    # a cycle if imported at module load. Local import is safe.
    from equity_tracker import get_live_equity, get_baseline_equity_for_today
    from trade_journal import get_realized_pnl_today

    equity   = get_live_equity()
    baseline = get_baseline_equity_for_today()

    if baseline is None or baseline <= 0:
        # No prior baseline → fall back to realized-only (won't see open-position swings)
        return get_realized_pnl_today()

    return equity - baseline


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

    pnl = calculate_daily_pnl()                       # network I/O — outside the lock
    cfg = active_config(equity)
    loss_pct = cfg["daily_loss_limit_pct"] if cfg else DAILY_LOSS_LIMIT_PCT
    limit = -abs(equity * loss_pct)
    halt_now = pnl <= limit

    # Persist under a cross-process lock so we re-read the latest state and
    # don't clobber a halt another strategy process set while we were computing.
    with file_lock(STATE_FILE):
        s = load_state()
        if s.get("halted"):
            # Someone else hit the limit first — respect it, don't un-halt.
            return True, s.get("realized_pnl", pnl)
        s["realized_pnl"] = pnl
        if halt_now:
            s["halted"] = True
        save_state(s)

    if halt_now:
        _log_halt_event(equity, pnl, limit)
        # Push to Telegram / email / Discord. Lazy import to keep notifier
        # optional — if event_notifier has a config error the halt itself
        # must still register.
        try:
            from event_notifier import notify_halt_triggered
            notify_halt_triggered(equity, pnl, limit)
        except Exception as e:
            print(f"[RiskManager] notify_halt_triggered failed: {e}")
        print(
            f"[RiskManager] ⛔ DAILY LOSS LIMIT HIT | "
            f"P&L: ${pnl:,.2f} | Limit: ${limit:,.2f} | Trading HALTED"
        )
        return True, pnl

    return False, pnl


# ─── Per-Strategy Capital Allocation ───────────────────────────────────────────

def deployed_capital_by_strategy() -> dict[str, float]:
    """
    Sum the cost basis of currently-open positions, grouped by strategy.

    Reads the local position files:
        open_positions.json   (catalyst + iv_rank if it records entries)
        hft_positions.json    (intraday)
        pead_positions.json   (post-earnings / news drift)

    Cost basis per leg = entry_price × |quantity| × 100. Returns
    {strategy: dollars_deployed}. Strategies whose positions aren't tracked
    locally (e.g. broker-only spread legs) won't be fully counted — the cap
    is best-effort for those.
    """
    from collections import defaultdict
    out: dict[str, float] = defaultdict(float)

    try:
        from position_manager import load_positions
        for _sym, d in load_positions().items():
            strat = d.get("strategy", "unknown")
            px  = float(d.get("entry_price", 0) or 0)
            qty = abs(int(d.get("quantity", 0) or 0))
            out[strat] += px * qty * 100
    except Exception as e:
        print(f"[RiskManager] deployed_capital: could not read open_positions: {e}")

    try:
        if os.path.exists("hft_positions.json"):
            with open("hft_positions.json", "r") as f:
                hft = json.load(f)
            for p in (hft if isinstance(hft, list) else []):
                px  = float(p.get("entry_price", 0) or 0)
                qty = abs(int(p.get("quantity", 0) or 0))
                out["hft_intraday"] += px * qty * 100
    except Exception as e:
        print(f"[RiskManager] deployed_capital: could not read hft_positions: {e}")

    try:
        if os.path.exists("pead_positions.json"):
            with open("pead_positions.json", "r") as f:
                pead = json.load(f)
            for p in (pead if isinstance(pead, list) else []):
                px  = float(p.get("entry_price", 0) or 0)
                qty = abs(int(p.get("quantity", 0) or 0))
                out["pead"] += px * qty * 100
    except Exception as e:
        print(f"[RiskManager] deployed_capital: could not read pead_positions: {e}")

    try:
        if os.path.exists("bounce_positions.json"):
            with open("bounce_positions.json", "r") as f:
                bounce = json.load(f)
            for p in (bounce if isinstance(bounce, list) else []):
                px  = float(p.get("entry_price", 0) or 0)
                qty = abs(int(p.get("quantity", 0) or 0))
                out["bounce"] += px * qty * 100
    except Exception as e:
        print(f"[RiskManager] deployed_capital: could not read bounce_positions: {e}")

    return dict(out)


def _strategy_budget(strategy: str | None, equity: float, base_budget: float) -> tuple[float, str | None]:
    """
    Clamp base_budget to the strategy's remaining capital allocation.

    Returns (allowed_budget, reject_reason). reject_reason is None when the
    strategy still has room; otherwise allowed_budget is 0 and reject_reason
    explains why.
    """
    alloc = (active_config(equity) or {}).get("strategy_allocation_pct") or STRATEGY_ALLOCATION_PCT
    if not strategy or strategy not in alloc:
        return base_budget, None

    cap = equity * alloc[strategy]
    deployed = deployed_capital_by_strategy().get(strategy, 0.0)
    remaining = cap - deployed

    if remaining <= 0:
        return 0.0, (f"{strategy} capital allocation full "
                     f"(${deployed:,.0f} deployed / ${cap:,.0f} cap)")

    return min(base_budget, remaining), None


def _bear_throttle(strategy: str | None, equity: float = 0.0) -> tuple[float, float, str | None]:
    """
    Risk adjustments for the current market regime.

    Returns (size_mult, cap_mult, reject_reason):
      • bull / unknown regime (or throttle off) → (1.0, 1.0, None) — a no-op.
      • bear regime → shrink the per-trade budget and the position cap, and (when
        BEAR_PAUSE_LONGS is set) reject new long-directional entries outright.

    BEAR_REGIME_THROTTLE is the global master switch; the active tier config can
    ADDITIONALLY disable the throttle for a given account size. Fails OPEN — if
    the regime can't be read it returns the no-op, so a transient data hiccup
    never blocks trading. The regime read is cached per day, so this is
    effectively free to call on every trade.
    """
    cfg = active_config(equity)
    tier_throttle = cfg["bear_regime_throttle"] if cfg else True
    if not BEAR_REGIME_THROTTLE or not tier_throttle or strategy in BEAR_THROTTLE_EXEMPT:
        return 1.0, 1.0, None
    try:
        from market_regime import is_bear_market
        if not is_bear_market():
            return 1.0, 1.0, None
    except Exception:
        return 1.0, 1.0, None

    reject = None
    if BEAR_PAUSE_LONGS and strategy in LONG_DIRECTIONAL_STRATEGIES:
        reject = "Bear regime — new long-directional entries paused (SPY < 200d SMA)"
    return BEAR_SIZE_MULT, BEAR_POSITION_MULT, reject


# ─── Drawdown governor ───────────────────────────────────────────────────────────

def load_drawdown_state() -> dict:
    if not os.path.exists(DRAWDOWN_STATE_FILE):
        return {}
    try:
        with open(DRAWDOWN_STATE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def update_high_water_mark(equity: float) -> float:
    """
    Ratchet the persisted high-water mark up to `equity` and return the current
    HWM. The first-ever call anchors the HWM at the current equity — so the
    governor measures drawdown from NOW, not from any historical peak. Delete
    drawdown_state.json to re-anchor.
    """
    if equity <= 0:
        return float(load_drawdown_state().get("hwm", 0) or 0)
    with file_lock(DRAWDOWN_STATE_FILE):
        st  = load_drawdown_state()
        hwm = float(st.get("hwm", 0) or 0)
        if equity > hwm:                       # new peak (also covers first run)
            st["hwm"]      = round(equity, 2)
            st["hwm_date"] = today_est().isoformat()
            st.setdefault("anchored_at", today_est().isoformat())
            atomic_write_json(DRAWDOWN_STATE_FILE, st)
            hwm = st["hwm"]
    return hwm


def _rolling_pnl_pct(days: int = WEEKLY_LOOKBACK_DAYS) -> float | None:
    """
    Trailing P&L over the last `days` trading days as a fraction (e.g. -0.08),
    using each day's CLOSING equity (last snapshot of the day). None if there
    isn't enough history. Lazy import avoids an equity_tracker import cycle.
    """
    try:
        from equity_tracker import load_equity_curve
        curve = load_equity_curve()
    except Exception:
        return None
    closes: dict[str, float] = {}
    for r in curve:
        d = r.get("date")
        e = float(r.get("equity", 0) or 0)
        if d and e > 0:
            closes[d] = e                      # last snapshot of each day wins
    series = [closes[d] for d in sorted(closes)]
    if len(series) < days + 1:
        return None
    past = series[-(days + 1)]
    if past <= 0:
        return None
    return (series[-1] - past) / past


def drawdown_governor(equity: float) -> tuple[float, str | None]:
    """
    Profit-protection layer. Returns (size_mult, reject_reason):
      • size_mult < 1.0   → de-risk new entries (graduated)
      • reject_reason set  → stop opening new positions entirely

    Measures drawdown from the high-water mark (ratcheted on every call) and the
    rolling weekly P&L. Existing positions are untouched — they ride to their own
    exits. Fails OPEN: any error returns (1.0, None) so a data hiccup never
    blocks trading.
    """
    if not DRAWDOWN_GOVERNOR or equity <= 0:
        return 1.0, None
    try:
        hwm = update_high_water_mark(equity)
        if hwm <= 0:
            return 1.0, None
        dd = (equity - hwm) / hwm              # <= 0

        # Hard stops first.
        if dd <= -DD_HALT_PCT:
            return 0.0, (f"Drawdown halt — down {abs(dd) * 100:.1f}% from peak "
                         f"${hwm:,.0f} (limit {DD_HALT_PCT * 100:.0f}%)")
        weekly = _rolling_pnl_pct()
        if weekly is not None and weekly <= -WEEKLY_LOSS_LIMIT_PCT:
            return 0.0, (f"Weekly loss limit — down {abs(weekly) * 100:.1f}% over "
                         f"{WEEKLY_LOOKBACK_DAYS} trading days "
                         f"(limit {WEEKLY_LOSS_LIMIT_PCT * 100:.0f}%)")

        # Graduated de-risk.
        if dd <= -DD_DERISK_QUARTER_PCT:
            return 0.25, None
        if dd <= -DD_DERISK_HALF_PCT:
            return 0.50, None
        return 1.0, None
    except Exception:
        return 1.0, None


def drawdown_status(equity: float) -> dict:
    """Snapshot of the governor state for the dashboard / logs.

    Runs the governor FIRST so the high-water mark is anchored/ratcheted before
    we read it back (otherwise the very first call would report hwm=0).
    """
    mult, reject = drawdown_governor(equity)
    hwm    = float(load_drawdown_state().get("hwm", 0) or 0)
    dd_pct = round((equity - hwm) / hwm * 100, 2) if hwm > 0 and equity > 0 else 0.0
    weekly = _rolling_pnl_pct()
    return {
        "hwm":          round(hwm, 2),
        "drawdown_pct": dd_pct,
        "weekly_pct":   round(weekly * 100, 2) if weekly is not None else None,
        "size_mult":    mult,
        "halted":       reject is not None,
        "reason":       reject,
    }


def _iv_mult_from_ctx(strategy: str | None, ctx: dict | None) -> float:
    """
    PURE: IV-aware size multiplier for a long-premium buyer given its IV context.
    1.0 (no change) unless the strategy is an IV-sized buyer AND options are rich.
    """
    if not IV_AWARE_SIZING or strategy not in IV_SIZED_STRATEGIES or not ctx:
        return 1.0
    if ctx.get("regime") != "rich":
        return 1.0
    rank  = ctx.get("iv_rank")
    ratio = ctx.get("iv_hv_ratio")
    very = ((rank is not None and rank >= IV_VERY_RICH_RANK) or
            (ratio is not None and ratio >= IV_VERY_RICH_RATIO))
    return IV_VERY_RICH_MULT if very else IV_RICH_MULT


def _iv_size_mult(strategy: str | None, ticker: str) -> float:
    """IV-aware size multiplier (1.0 = unchanged). FAILS OPEN if IV unavailable."""
    if not IV_AWARE_SIZING or strategy not in IV_SIZED_STRATEGIES:
        return 1.0
    try:
        from iv_provider import iv_context
        ctx = iv_context(ticker)
    except Exception:
        return 1.0
    return _iv_mult_from_ctx(strategy, ctx)


# ─── Full Pre-Trade Check ──────────────────────────────────────────────────────

def pre_trade_check(ticker: str, strategy: str | None = None) -> dict:
    """
    Run all risk checks before placing a trade.

    `strategy` (optional) enables the per-strategy capital cap: the returned
    budget is clamped to that strategy's remaining allocation, and the trade
    is rejected outright if the strategy is already fully deployed.

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

    # Operator control (bot_control / external Discord bot): a MANUAL halt or a
    # paused strategy blocks new entries immediately. Cheap (local file) and
    # checked first so it short-circuits before any network work. Fails open if
    # the control file is unreadable.
    try:
        from bot_control import control_block_reason
        control_reject = control_block_reason(strategy)
    except Exception:
        control_reject = None
    if control_reject:
        return _reject(control_reject, equity=equity, budget=0, pnl=0)

    # Account-size-aware tier config (tiers + dashboard overrides). None → the
    # module-constant fallback path, which behaves exactly like the standard tier.
    cfg = active_config(equity)

    # Strategy enabled for this tier? The small/micro tiers disable intraday
    # day-trading (PDT rule under $25k) and the negative-EV catalyst engine.
    # A disabled strategy may still MONITOR and exit its open positions — it
    # just can't OPEN new ones, so nothing gets stranded unmanaged.
    if cfg and strategy and strategy not in cfg["enabled_strategies"]:
        return _reject(
            f"{strategy} disabled for '{cfg['tier']}' tier (account ${equity:,.0f})",
            equity=equity, budget=0, pnl=0,
        )

    # Daily loss limit
    halted, daily_pnl = check_daily_loss_limit(equity)
    if halted:
        return _reject(
            f"Daily loss limit hit (P&L: ${daily_pnl:,.2f})",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Bear-market throttle — de-risk (and optionally pause longs) in a downtrend.
    size_mult, cap_mult, regime_reject = _bear_throttle(strategy, equity)
    if regime_reject:
        return _reject(regime_reject, equity=equity, budget=0, pnl=daily_pnl)

    # Drawdown governor — protect profits from a slow multi-day bleed the daily
    # halt misses. De-risks new entries as equity falls from its peak, then halts
    # them past the hard limit. Bounce (bear-market offense) is exempt, same as
    # the bear throttle, so it can still hunt capitulation in a deep drawdown.
    if strategy in BEAR_THROTTLE_EXEMPT:
        dd_mult, dd_reject = 1.0, None
    else:
        dd_mult, dd_reject = drawdown_governor(equity)
    if dd_reject:
        return _reject(dd_reject, equity=equity, budget=0, pnl=daily_pnl)

    # Max open positions — enforced PER TRADE TYPE so swing and day trades have
    # independent budgets (a full swing book doesn't block intraday day trades).
    # Caps come from the active tier (smaller accounts hold fewer positions) and
    # are further shrunk in a bear regime (cap_mult < 1).
    trade_type = trade_type_for(strategy)
    if cfg:
        base_cap = cfg["max_day_positions"] if trade_type == "day" else cfg["max_swing_positions"]
    else:
        base_cap = MAX_DAY_POSITIONS if trade_type == "day" else MAX_SWING_POSITIONS
    type_cap   = max(1, int(base_cap * cap_mult))
    type_count = count_positions_by_type(trade_type)
    if type_count >= type_cap:
        return _reject(
            f"Max {trade_type} positions reached ({type_count}/{type_cap})",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Duplicate position guard
    if is_already_in_position(ticker):
        return _reject(
            f"Already in position on {ticker}",
            equity=equity, budget=0, pnl=daily_pnl
        )

    # Correlation / concentration guard — don't stack the book into one cluster.
    concentration = concentration_reject(ticker)
    if concentration:
        return _reject(concentration, equity=equity, budget=0, pnl=daily_pnl)

    # Portfolio vega guard — don't pile more volatility exposure on the same side
    # once the book is already lopsided (fails open if greeks unavailable).
    vega = _vega_reject(strategy, equity)
    if vega:
        return _reject(vega, equity=equity, budget=0, pnl=daily_pnl)

    # Calculate budget, then clamp to the strategy's capital allocation.
    budget = get_position_size(equity)
    budget, alloc_reject = _strategy_budget(strategy, equity, budget)
    if alloc_reject:
        return _reject(alloc_reject, equity=equity, budget=0, pnl=daily_pnl)
    budget *= size_mult   # bear-regime throttle shrinks the final budget
    budget *= dd_mult     # drawdown governor de-risk (graduated)

    # IV-aware sizing — long-premium buyers trade smaller when options are rich.
    iv_mult = _iv_size_mult(strategy, ticker)
    budget *= iv_mult

    print(
        f"[RiskManager] ✅ {ticker} approved | "
        f"Equity: ${equity:,.2f} | Budget: ${budget:,.2f} | "
        f"Daily P&L: ${daily_pnl:,.2f} | {trade_type.capitalize()} positions: {type_count}/{type_cap}"
        + (f" | Strategy: {strategy}" if strategy else "")
        + (" | ⚠ BEAR throttle" if size_mult < 1.0 else "")
        + (f" | ⚠ drawdown ×{dd_mult:.2f}" if dd_mult < 1.0 else "")
        + (f" | ⚠ IV-rich size ×{iv_mult:.1f}" if iv_mult < 1.0 else "")
    )

    return {
        "approved": True,
        "reason":   "All checks passed",
        "equity":   equity,
        "budget":   budget,
        "daily_pnl": daily_pnl,
    }


def record_trade(ticker: str):
    """Record that a trade was placed today.

    Wrapped in a cross-process lock so two strategies placing trades at the
    same time can't both read trades_today=N and both write N+1 (lost update).
    """
    with file_lock(STATE_FILE):
        state = load_state()
        state["trades_today"] = state.get("trades_today", 0) + 1
        save_state(state)
    print(f"[RiskManager] Trade recorded for {ticker} | Total today: {state['trades_today']}")


def reconcile_positions_from_broker() -> int:
    """
    Walk local open_positions.json and verify each one is still open at
    the broker. Anything in the local file but missing from the broker
    was closed outside the bot (manual close, expiry assignment, broker
    auto-close) — we journal it as closed_externally and drop it from
    the local file so the bot doesn't think it still owns it.

    Returns the count of stale positions reconciled (mostly for logging).
    """
    # Local imports to avoid cycles at module load.
    from position_manager import load_positions, remove_position

    try:
        broker_pos    = get_open_positions()
        broker_syms   = {p.get("symbol") for p in broker_pos if p.get("symbol")}
        local_pos     = load_positions()
    except Exception as e:
        print(f"[RiskManager] reconcile_positions_from_broker: could not query broker: {e}")
        return 0

    stale = [sym for sym in local_pos if sym not in broker_syms]
    for sym in stale:
        print(f"[RiskManager] Stale local position {sym} not at broker — journaling as closed_externally")
        try:
            # remove_position resolves an exit price from the chain when None
            # is passed, journals the close, and deletes from open_positions.
            remove_position(sym, exit_price=None, exit_reason="closed_externally")
        except Exception as e:
            print(f"[RiskManager]   reconcile of {sym} failed: {e}")

    # Untracked broker positions (interesting but non-fatal — show them on the
    # dashboard via build_positions_data's "broker" fallback path).
    untracked = [s for s in broker_syms if s and s not in local_pos and len(s) > 6]
    if untracked:
        print(f"[RiskManager] {len(untracked)} broker position(s) not in local file (shown as 'broker' on dashboard)")

    return len(stale)


def reconcile_from_broker() -> None:
    """
    Called once at startup to sync risk state with the broker.

    Two things happen:
      1. Position reconciliation — any locally-tracked positions that have
         been closed at the broker (manual close, expiry, etc.) are
         journaled and removed locally, so the bot doesn't double-count
         them as open or skip re-entry on the underlying.
      2. Halt-state reconciliation — re-runs the daily loss check against
         live equity, restoring the halt flag if the limit was already
         breached today.
    """
    # Position reconciliation first so the equity snapshot below reflects
    # any unrealized-→-realized conversion the closed_externally journal
    # entries are about to cause.
    n_stale = reconcile_positions_from_broker()
    if n_stale:
        print(f"[RiskManager] Reconciled {n_stale} stale local position(s)")

    balances = get_account_balance()
    equity = balances.get("total_equity", 0)
    if equity <= 0:
        print("[RiskManager] reconcile_from_broker: could not fetch equity — skipping halt check")
        return
    halted, pnl = check_daily_loss_limit(equity)
    status = "HALTED" if halted else "OK"
    print(
        f"[RiskManager] Reconciled | Equity: ${equity:,.2f} | "
        f"Daily P&L: ${pnl:,.2f} | Status: {status}"
    )


def _reject(reason: str, equity: float, budget: float, pnl: float) -> dict:
    print(f"[RiskManager] ❌ Trade rejected: {reason}")
    return {
        "approved":  False,
        "reason":    reason,
        "equity":    equity,
        "budget":    budget,
        "daily_pnl": pnl,
    }
