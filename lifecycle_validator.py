"""
lifecycle_validator.py — end-to-end paper-trade validation against Tradier sandbox.

Everything the bot does to OPEN and CLOSE a trade has only ever run in MOCK_MODE
or unit tests. This script exercises the real path against the Tradier SANDBOX:

    place_and_confirm (buy) → poll for real fill → verify broker position
        → record_entry → place_and_confirm (sell) → verify fill
        → remove_position (journals it) → verify gone + journaled

It proves the order-status polling, real fill-price capture, position tracking,
and journaling all work against a live (paper) broker — the things mocks can't.

SAFETY
------
  * Pre-flight checks are READ-ONLY and always run.
  * The part that actually PLACES ORDERS is gated:
        - refuses to run unless TRADIER_SANDBOX=true   (never touches live)
        - refuses in MOCK_MODE (nothing to validate)
        - requires --run AND a typed "RUN" confirmation (or --run --force)
  * Uses 1 contract of a liquid SPY call, minimal capital.
  * A finally-block flattens the test position even if a step fails, so it
    never leaves a dangling paper position behind.

USAGE
-----
    python lifecycle_validator.py              # pre-flight only (safe, read-only)
    python lifecycle_validator.py --run        # full lifecycle (asks to confirm)
    python lifecycle_validator.py --run --force # full lifecycle, no prompt
"""

from __future__ import annotations

import argparse
import sys
import time
import datetime

import requests

import tradier_client as tc
from tradier_client import (
    get_account_balance, get_open_positions, get_options_expirations,
    get_options_chain, get_quote, MOCK_MODE, TRADIER_SANDBOX,
)
from order_manager import place_and_confirm
from position_manager import record_entry, remove_position, load_positions
from trade_journal import load_closed_trades

TEST_UNDERLYING = "SPY"        # most liquid optionable name
TEST_QTY = 1                   # always 1 contract
MAX_COST_PER_CONTRACT = 3000   # don't pick anything absurdly expensive for a test
MIN_DTE = 2                    # avoid 0-DTE for a clean round trip


# ─── tiny reporter ────────────────────────────────────────────────────────────

_fail = 0


def ok(msg: str, detail: str = ""):
    print(f"  [PASS]  {msg}" + (f" — {detail}" if detail else ""))


def warn(msg: str, detail: str = ""):
    print(f"  [WARN]  {msg}" + (f" — {detail}" if detail else ""))


def bad(msg: str, detail: str = ""):
    global _fail
    _fail += 1
    print(f"  [FAIL]  {msg}" + (f" — {detail}" if detail else ""))


def section(title: str):
    print(f"\n{title}\n  " + "-" * 56)


# ─── helpers ──────────────────────────────────────────────────────────────────

def option_buying_power() -> float:
    """Read option buying power directly (the balance wrapper zeroes cash on
    margin accounts)."""
    try:
        url = f"{tc.BASE_URL}/accounts/{tc.TRADIER_ACCOUNT_ID}/balances"
        r = requests.get(url, headers=tc.HEADERS, timeout=10)
        r.raise_for_status()
        b = r.json().get("balances", {})
        margin = b.get("margin", {})
        if isinstance(margin, dict) and margin.get("option_buying_power") is not None:
            return float(margin["option_buying_power"])
        return float(b.get("total_cash", 0) or 0)
    except Exception:
        return 0.0


def pick_test_contract() -> dict | None:
    """Select a liquid, near-ATM SPY call for the round-trip test."""
    exps = get_options_expirations(TEST_UNDERLYING)
    if not exps:
        return None
    today = datetime.date.today()
    dated = []
    for e in exps:
        try:
            dte = (datetime.date.fromisoformat(e) - today).days
            if dte >= MIN_DTE:
                dated.append((dte, e))
        except Exception:
            continue
    if not dated:
        return None
    dated.sort()
    _, expiration = dated[0]

    spot = get_quote(TEST_UNDERLYING)
    if spot <= 0:
        return None

    chain = get_options_chain(TEST_UNDERLYING, expiration)
    calls = [
        c for c in chain
        if c.get("option_type") == "call"
        and float(c.get("bid", 0) or 0) > 0
        and float(c.get("ask", 0) or 0) > 0
        and int(c.get("open_interest", 0) or 0) >= 100
    ]
    if not calls:
        return None

    # Nearest-to-ATM, then tightest relative spread, then affordable.
    calls.sort(key=lambda c: abs(float(c.get("strike", 0)) - spot))
    for c in calls[:10]:
        bid = float(c["bid"]); ask = float(c["ask"])
        # Reject crossed/locked quotes (ask <= bid). The Tradier sandbox returns
        # stale, inverted quotes outside market hours; a crossed market makes our
        # limit prices nonsensical and the round trip wouldn't fill realistically.
        if ask <= bid:
            continue
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        spread_pct = (ask - bid) / mid
        cost = mid * 100 * TEST_QTY
        if spread_pct <= 0.20 and cost <= MAX_COST_PER_CONTRACT:
            return {
                "symbol": c.get("symbol"),
                "strike": float(c.get("strike", 0)),
                "expiration": expiration,
                "bid": bid, "ask": ask, "mid": round(mid, 2),
                "spread_pct": round(spread_pct * 100, 1),
                "cost": round(cost, 2),
                "spot": spot,
            }
    return None


# ─── pre-flight (read-only) ───────────────────────────────────────────────────

def preflight() -> dict | None:
    """Run read-only checks. Returns the chosen test contract, or None on abort."""
    section("Pre-flight (read-only)")

    if MOCK_MODE:
        bad("Broker credentials", "MOCK_MODE — set TRADIER_API_KEY / TRADIER_ACCOUNT_ID to validate")
        return None
    ok("Broker credentials present")

    if not TRADIER_SANDBOX:
        bad("Sandbox guard", "TRADIER_SANDBOX is FALSE — refusing to run a live-money lifecycle test")
        return None
    ok("Sandbox mode", "TRADIER_SANDBOX=true (paper money)")

    bal = get_account_balance()
    equity = float(bal.get("total_equity", 0) or 0)
    if equity <= 0:
        bad("Account equity", "could not fetch a positive equity")
        return None
    bp = option_buying_power()
    ok("Account", f"equity ${equity:,.0f} | option buying power ${bp:,.0f}")

    # Market-hours awareness — the sandbox returns stale/crossed quotes outside
    # RTH, which is both why a contract may not be selectable and why fills
    # would be unrealistic. Check this BEFORE contract selection so the message
    # is helpful.
    from utils import now_est
    n = now_est()
    is_rth = (n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) <= (16, 0))
    if is_rth:
        ok("Market hours", f"regular trading hours ({n.strftime('%H:%M')} ET)")
    else:
        warn("Market hours", f"market closed ({n.strftime('%a %H:%M')} ET) — sandbox quotes are stale/crossed "
                             "and fills are unrealistic; run during RTH for a meaningful test")

    contract = pick_test_contract()
    if not contract:
        bad("Test contract", f"no liquid {TEST_UNDERLYING} call with a valid (uncrossed) quote — "
                             "common outside market hours; retry during RTH")
        return None
    ok("Test contract selected",
       f"{contract['symbol']} | strike ${contract['strike']:.0f} | "
       f"mid ${contract['mid']:.2f} (bid {contract['bid']} / ask {contract['ask']}, "
       f"{contract['spread_pct']}% wide) | est cost ${contract['cost']:.0f}")

    if contract["cost"] > bp:
        bad("Buying power", f"test cost ${contract['cost']:.0f} exceeds option BP ${bp:,.0f}")
        return None
    ok("Buying power sufficient for the test")

    now = datetime.datetime.now()
    if now.weekday() >= 5:
        warn("Market hours", "weekend — sandbox may not fill orders until the next session")
    ok("Pre-flight complete")
    return contract


# ─── lifecycle (gated — places real paper orders) ─────────────────────────────

def run_lifecycle(contract: dict) -> None:
    symbol = contract["symbol"]
    section("Lifecycle test — placing PAPER orders")
    print(f"  Test contract: {symbol}  x{TEST_QTY}\n")

    opened = False
    try:
        # 1) BUY — place and confirm the real fill
        buy = place_and_confirm(
            symbol=TEST_UNDERLYING, option_symbol=symbol, side="buy_to_open",
            quantity=TEST_QTY, order_type="limit",
            price=round(contract["ask"], 2),     # cross the spread to fill
            strategy="lifecycle_test", fallback_price=contract["mid"], timeout=45.0,
        )
        if not buy.ok or buy.filled_qty <= 0:
            bad("BUY fill", f"order did not fill: {buy.reason}")
            return
        opened = True
        ok("BUY filled", f"{buy.filled_qty} @ ${buy.avg_fill_price:.2f} (order {buy.order_id}, status {buy.status})")
        if buy.avg_fill_price <= 0:
            warn("Fill price", "broker reported no avg_fill_price — fell back to mid")

        # 2) Broker shows the position
        time.sleep(1.0)
        broker_syms = {p.get("symbol") for p in get_open_positions()}
        if symbol in broker_syms:
            ok("Broker position confirmed", symbol)
        else:
            warn("Broker position", "not visible yet (sandbox propagation lag is common)")

        # 3) Record into the local book
        record_entry(
            option_symbol=symbol, underlying=TEST_UNDERLYING,
            entry_price=buy.avg_fill_price or contract["mid"], quantity=buy.filled_qty,
            expiration=contract["expiration"], strategy="lifecycle_test",
        )
        if symbol in load_positions():
            ok("record_entry", "position written to open_positions.json")
        else:
            bad("record_entry", "position not found in open_positions.json after write")

        n_closed_before = len(load_closed_trades())

        # 4) SELL — close it and confirm the fill
        time.sleep(1.0)
        sell = place_and_confirm(
            symbol=TEST_UNDERLYING, option_symbol=symbol, side="sell_to_close",
            quantity=buy.filled_qty, order_type="limit",
            price=round(contract["bid"], 2),     # cross down to fill
            strategy="lifecycle_test", fallback_price=contract["mid"], timeout=45.0,
        )
        if not sell.ok or sell.filled_qty <= 0:
            bad("SELL fill", f"close did not fill: {sell.reason} (position still open — see cleanup)")
            return
        ok("SELL filled", f"{sell.filled_qty} @ ${sell.avg_fill_price:.2f} (order {sell.order_id})")

        # 5) Journal + remove from local book
        remove_position(symbol, exit_price=sell.avg_fill_price, exit_reason="lifecycle_test")
        opened = False  # closed cleanly
        if symbol not in load_positions():
            ok("remove_position", "removed from open_positions.json")
        else:
            bad("remove_position", "still present in open_positions.json")

        # 6) Verify it was journaled
        closed = load_closed_trades()
        if len(closed) > n_closed_before and any(t.get("option_symbol") == symbol for t in closed[-3:]):
            rec = next(t for t in reversed(closed) if t.get("option_symbol") == symbol)
            ok("Journaled to closed_trades.json",
               f"P&L ${rec.get('pnl_dollar', 0):+.2f} ({rec.get('pnl_pct', 0):+.1f}%) — "
               f"spread cost on a buy-high/sell-low round trip is expected")
        else:
            bad("Journaling", "no closed-trade record found for the test symbol")

        # 7) Broker no longer shows it
        time.sleep(1.0)
        if symbol not in {p.get("symbol") for p in get_open_positions()}:
            ok("Broker flat", "test position closed at broker")
        else:
            warn("Broker flat", "still showing (propagation lag) — verify on the dashboard")

    finally:
        # Safety net: if we opened but didn't cleanly close, flatten now.
        if opened:
            warn("Cleanup", f"test left {symbol} open — attempting to flatten")
            try:
                flat = place_and_confirm(
                    symbol=TEST_UNDERLYING, option_symbol=symbol, side="sell_to_close",
                    quantity=TEST_QTY, order_type="market",
                    strategy="lifecycle_test", timeout=30.0,
                )
                if flat.ok:
                    remove_position(symbol, exit_price=flat.avg_fill_price, exit_reason="lifecycle_cleanup")
                    ok("Cleanup", "test position flattened")
                else:
                    bad("Cleanup", f"could NOT flatten {symbol} — close it manually or run kill_switch.py")
            except Exception as e:
                bad("Cleanup", f"error flattening {symbol}: {e} — close it manually")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end sandbox lifecycle validation.")
    parser.add_argument("--run", action="store_true",
                        help="Actually place paper orders (default: pre-flight only).")
    parser.add_argument("--force", action="store_true",
                        help="Skip the typed confirmation when used with --run.")
    args = parser.parse_args()

    print("=" * 60)
    print("  MawiTek — Lifecycle Validator")
    print("=" * 60)

    contract = preflight()
    if contract is None:
        print("\nPre-flight failed — not proceeding.")
        return 1

    if not args.run:
        print("\nPre-flight OK. This was READ-ONLY — no orders were placed.")
        print("To run the full paper round-trip:  python lifecycle_validator.py --run")
        return 0

    if not args.force:
        print(f"\nThis will place a REAL paper order: buy + sell {TEST_QTY}x {contract['symbol']} "
              f"(~${contract['cost']:.0f}) in the Tradier SANDBOX.")
        confirm = input('Type RUN to proceed: ').strip()
        if confirm != "RUN":
            print("Aborted — no orders placed.")
            return 1

    run_lifecycle(contract)

    print("\n" + "=" * 60)
    print(f"  Lifecycle validation finished — {_fail} failure(s)")
    print("=" * 60)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
