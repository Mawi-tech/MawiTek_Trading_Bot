"""
setup_tracker.py — does the scanner actually have an edge?

The scanners surface setups onto an accumulating board (scanner_setups.json),
each with the date/time it was found. This module closes the loop: it stamps a
reference price when a setup first appears, then measures the underlying's
DIRECTIONAL forward return (favorable for the setup's predicted direction) and,
after a per-style horizon, finalizes each as a win / loss / flat.

Aggregating those outcomes by setup-score bucket answers the key question — does
a higher scanner score actually predict a bigger, more reliable move? — for ALL
surfaced setups, including the ones the bot never traded. That's the feedback
loop for tuning the scanners.

It measures the UNDERLYING's move (not the option's) on purpose: that's the clean
read on whether the scanner picked names that moved the right way, without option
greeks/IV noise. Quotes are batch-fetched (one API call for the whole board).
"""

from mawitek.infra.utils import now_est, parse_isodt
from mawitek.infra.state_io import read_json, atomic_write_json, file_lock

SCANNER_SETUPS_FILE = "scanner_setups.json"

# How long after a setup is first priced before we finalize its outcome.
HORIZON_HOURS = {"day": 6.0, "swing": 5 * 24.0}   # day ≈ same session, swing ≈ 5 days
HORIZON_DEFAULT_HOURS = 5 * 24.0

# Directional move (%) needed to score a setup a hit / miss; in between is flat.
WIN_THRESHOLD_PCT = 2.0

SCORE_BUCKETS = (("45-59", 45, 60), ("60-74", 60, 75), ("75+", 75, 10_000))


def _direction_sign(setup: dict) -> float:
    """+1 for a bullish setup, -1 for bearish (so forward return is favorable-positive)."""
    d = setup.get("trade_direction") or setup.get("direction") or "bullish"
    return -1.0 if str(d).lower().startswith("bear") else 1.0


def track_setups(setups: list[dict], quotes: dict[str, float] | None = None) -> list[dict]:
    """
    Update each not-yet-finalized setup with a reference price (first sighting),
    its directional forward return, and — once past its horizon — a finalized
    win/loss/flat outcome. Mutates and returns the list.

    `quotes` may be injected (tests); otherwise the current quotes for every
    pending underlying are batch-fetched in one call.
    """
    pending = [s for s in setups if not s.get("perf_finalized")]
    if quotes is None:
        syms = sorted({s.get("ticker") for s in pending if s.get("ticker")})
        if syms:
            from mawitek.data.tradier_client import get_quotes
            quotes = get_quotes(syms)
        else:
            quotes = {}

    now = now_est()
    for s in pending:
        px = quotes.get(s.get("ticker"))
        if not px or px <= 0:
            continue

        if not s.get("ref_price"):
            s["ref_price"] = round(px, 4)
            s["ref_at"] = now.isoformat()

        ref = s.get("ref_price")
        if not ref:
            continue

        fwd = round(_direction_sign(s) * (px - ref) / ref * 100, 2)
        s["forward_return_pct"] = fwd
        s["last_price"] = round(px, 4)

        try:
            age_h = (now - parse_isodt(s["ref_at"])).total_seconds() / 3600
        except Exception:
            age_h = 0.0
        horizon = HORIZON_HOURS.get(s.get("trade_style", "swing"), HORIZON_DEFAULT_HOURS)
        if age_h >= horizon:
            s["perf_finalized"] = True
            s["outcome"] = ("win" if fwd >= WIN_THRESHOLD_PCT
                            else "loss" if fwd <= -WIN_THRESHOLD_PCT else "flat")
            s["outcome_return_pct"] = fwd

    return setups


def track_and_persist(setups: list[dict]) -> list[dict]:
    """track_setups + write the enriched board back to scanner_setups.json (locked)."""
    setups = track_setups(setups)
    try:
        with file_lock(SCANNER_SETUPS_FILE):
            saved = read_json(SCANNER_SETUPS_FILE, {})
            ts = saved.get("timestamp") if isinstance(saved, dict) else None
            atomic_write_json(SCANNER_SETUPS_FILE, {"timestamp": ts, "setups": setups})
    except Exception as e:
        print(f"[SetupTracker] Could not persist tracked setups: {e}")
    return setups


def _bucket(score: float) -> str:
    for name, lo, hi in SCORE_BUCKETS:
        if lo <= score < hi:
            return name
    return SCORE_BUCKETS[0][0]


def _summarize(rows: list[dict]) -> dict:
    """{n, hit_rate, avg_return} for a set of FINALIZED setups."""
    wins = sum(1 for s in rows if s.get("outcome") == "win")
    losses = sum(1 for s in rows if s.get("outcome") == "loss")
    decided = wins + losses
    avg = round(sum(s.get("outcome_return_pct", 0) for s in rows) / len(rows), 2) if rows else None
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "hit_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_return": avg,
    }


def scanner_performance(setups: list[dict]) -> dict:
    """
    Aggregate scanner edge from the tracked board. PURE. Returns overall hit
    rate / avg directional return (finalized setups) plus a breakdown by
    score bucket — the "does a higher score predict a better move?" view.
    """
    finalized = [s for s in setups if s.get("perf_finalized")]
    open_tracked = [s for s in setups
                    if not s.get("perf_finalized") and s.get("forward_return_pct") is not None]

    overall = _summarize(finalized)
    by_score = {name: _summarize([s for s in finalized if _bucket(s.get("setup_score", 0)) == name])
                for name, _lo, _hi in SCORE_BUCKETS}

    return {
        "tracked":   len(finalized) + len(open_tracked),
        "finalized": len(finalized),
        "open":      len(open_tracked),
        "hit_rate":  overall["hit_rate"],
        "avg_return": overall["avg_return"],
        "wins":      overall["wins"],
        "losses":    overall["losses"],
        "by_score":  by_score,
        "win_threshold": WIN_THRESHOLD_PCT,
    }
