"""
signal_publisher.py — the PUBLIC, account-agnostic signal feed for a signal
service.

SECURITY MODEL (read this before changing anything):
    The bot runs on the OWNER's real account. Subscribers must NEVER see the
    owner's account data — equity, dollar P&L, position SIZES, balances, buying
    power, broker keys, or account IDs. Those live in owner-private artifacts
    (dashboard_state.json, .env, the *_positions.json books) served only on the
    loopback / Tailscale dashboard.

    A published signal is the RECOMMENDATION only: ticker, strategy, direction,
    structure (strike/expiry/option type), conviction, rationale, and the
    PERCENTAGE result. No dollars, no contract counts, no equity — ever.

    The boundary is enforced in ONE place (sanitize()) and FAILS CLOSED: if a
    forbidden field ever reaches it, it raises instead of leaking. test_signal_
    publisher.py serializes the whole feed and asserts no account data appears.

This module is READ-ONLY w.r.t. the trading engine: it projects existing
journals into a public feed. It never imports tradier_client and never touches
the executors, so it cannot affect live trading.
"""

import json
import os
from typing import Any

from mawitek.infra.state_io import atomic_write_json

PUBLIC_FEED_FILE = "public_feed.json"

# The ONLY fields a published signal may contain. Anything not here is dropped.
PUBLIC_FIELDS = frozenset({
    "id", "ts", "status", "ticker", "strategy", "trade_type", "direction",
    "structure", "strike", "expiration", "dte", "conviction", "rationale",
    "setup_score", "result_pct",
})

# Defense-in-depth denylist. Any key whose lowercased name contains one of these
# substrings is account/owner data and must never appear in the public feed.
# sanitize() RAISES if it sees one, so a future bug can't silently leak.
FORBIDDEN_SUBSTRINGS = (
    "dollar", "equity", "balance", "cash", "buying_power", "account",
    "quantity", "qty", "contracts", "entry_price", "exit_price", "cost",
    "margin", "credit", "debit", "notional", "size", "capital", "deployed",
    "api_key", "token", "secret", "password", "webhook",
)


def _has_forbidden_key(obj: Any) -> str | None:
    """Return the first forbidden key found anywhere in obj, else None."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            for bad in FORBIDDEN_SUBSTRINGS:
                if bad in kl:
                    return str(k)
            hit = _has_forbidden_key(v)
            if hit:
                return hit
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            hit = _has_forbidden_key(item)
            if hit:
                return hit
    return None


def sanitize(record: dict) -> dict:
    """
    Reduce a record to the public allowlist and FAIL CLOSED on any leak.

    Raises ValueError if a forbidden (account-private) key is present, so a
    mistake upstream surfaces loudly instead of leaking owner data.
    """
    bad = _has_forbidden_key(record)
    if bad is not None:
        raise ValueError(
            f"refusing to publish: record contains forbidden field '{bad}' "
            "(account-private data must never reach the public feed)"
        )
    return {k: v for k, v in record.items() if k in PUBLIC_FIELDS}


def _signal_id(rec: dict) -> str:
    """Stable id from non-sensitive identity fields (no PII, no account data)."""
    basis = "|".join(str(rec.get(k, "")) for k in
                     ("underlying", "strategy", "entry_time", "expiration"))
    # Deterministic, dependency-free, and reveals nothing.
    return f"sig_{abs(hash(basis)) % (10**12):012d}"


def _structure(sig: dict, strategy: str) -> str:
    """Human-readable structure label from the signal block."""
    detail = sig.get("strategy_detail")
    if detail:
        return str(detail)                       # e.g. iron_condor, bull_put_spread
    ot = sig.get("option_type")
    return f"long_{ot}" if ot else "option"


def to_public_signal(trade: dict) -> dict:
    """
    Project a closed-trade journal row into a sanitized public signal.

    Pulls ONLY the recommendation + percentage result. Dollar fields
    (pnl_dollar, entry_price, exit_price, quantity) are never read.
    """
    sig = trade.get("signals") or {}
    rec = {
        "id":          _signal_id(trade),
        "ts":          trade.get("exit_time") or trade.get("entry_time"),
        "status":      "closed",
        "ticker":      trade.get("underlying"),
        "strategy":    trade.get("strategy"),
        "trade_type":  trade.get("trade_type"),
        "direction":   sig.get("direction"),
        "structure":   _structure(sig, trade.get("strategy", "")),
        "strike":      sig.get("strike"),
        "expiration":  trade.get("expiration"),
        "dte":         sig.get("dte"),
        "conviction":  sig.get("conviction"),
        "rationale":   trade.get("exit_reason"),
        "setup_score": trade.get("setup_score"),
        "result_pct":  trade.get("pnl_pct"),     # PERCENT only — never pnl_dollar
    }
    return sanitize(rec)


def public_track_record(trades: list[dict]) -> dict:
    """
    Percentage-only performance summary. Computed from pnl_pct exclusively —
    no equity, no dollar P&L, so it reveals nothing about account size.
    """
    pcts = [float(t.get("pnl_pct", 0) or 0) for t in trades if t.get("pnl_pct") is not None]
    if not pcts:
        return {"trades": 0, "win_rate_pct": None, "avg_return_pct": None,
                "profit_factor": None}
    wins   = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p < 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades":         len(pcts),
        "win_rate_pct":   round(len(wins) / len(pcts) * 100, 1),
        "avg_return_pct": round(sum(pcts) / len(pcts), 2),
        "profit_factor":  round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
    }


def build_public_feed(closed_trades_file: str = "closed_trades.json") -> dict:
    """
    Build the full public feed from the (private) closed-trade journal.

    Returns {"signals": [...sanitized...], "track_record": {...%...}}. Every
    signal passes through sanitize(); the whole structure is re-checked before
    return so a leak can't slip past.
    """
    trades: list[dict] = []
    if os.path.exists(closed_trades_file):
        try:
            with open(closed_trades_file) as f:
                data = json.load(f)
            trades = data if isinstance(data, list) else []
        except Exception:
            trades = []

    signals = [to_public_signal(t) for t in trades]
    feed = {"signals": signals, "track_record": public_track_record(trades)}

    # Final belt-and-braces sweep over the assembled feed.
    leak = _has_forbidden_key(feed)
    if leak is not None:
        raise ValueError(f"public feed assembly leaked forbidden field '{leak}'")
    return feed


def write_public_feed(path: str = PUBLIC_FEED_FILE,
                      closed_trades_file: str = "closed_trades.json") -> dict:
    """Build and atomically persist the public feed. Returns the feed."""
    feed = build_public_feed(closed_trades_file)
    atomic_write_json(path, feed)
    return feed


if __name__ == "__main__":
    f = write_public_feed()
    print(f"[SignalPublisher] Wrote {PUBLIC_FEED_FILE}: "
          f"{len(f['signals'])} signals | track record: {f['track_record']}")
