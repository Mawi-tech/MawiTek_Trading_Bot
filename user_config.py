"""
user_config.py — account-size TIERS + user-customizable risk config.

WHY THIS EXISTS
---------------
The bot's risk rules (risk_manager.py) were all written for the ~$100k sandbox.
Percentage-based sizing and a 13-slot position book don't survive contact with a
small LIVE account: 3% of $3k is $90 — not even one option contract — and 5
intraday day-trades a week violate the Pattern-Day-Trader rule under $25k.

So instead of one fixed config we have ACCOUNT-SIZE TIERS. Each tier is a
sensible preset (risk %, position caps, which strategies run, capital splits)
matched to how much capital is actually in the account. The tier is picked
AUTOMATICALLY from live equity, or the user can pin one. On top of the tier the
user may override individual fields from the dashboard.

    effective config = TIER preset  ◄merged◄  user overrides  ◄clamped► safe bounds

KEEP-IT-AS-IT-IS GUARANTEE
--------------------------
The "standard" tier is byte-for-byte the current risk_manager defaults, and
`select_tier` returns "standard" for any account >= $25k. So with no
user_config.json on disk (the default), a normal-sized account behaves EXACTLY
as it does today. Tiers only change behaviour on a small account or when the
user deliberately customizes.

SAFETY (this file is written from a web form)
---------------------------------------------
user_config.json is written by the dashboard over HTTP. It controls real-money
risk, so EVERY value is clamped to a safe range on BOTH write (save_user_config)
AND read (effective_config) — never trust the file. Hard invariants that no
dashboard input can break:
  * risk-per-trade is capped at 10% and floored at 0.5%
  * the daily-loss halt can NEVER be disabled (1%..10%)
  * position caps are bounded integers
  * only known strategies may be enabled / allocated
Unknown keys are ignored. A malformed file falls back to the tier preset.

All reads are cached on the file's mtime, so the five strategy processes can
call effective_config() on every trade check without hammering the disk, yet a
dashboard save is picked up within one cycle (no restart needed).
"""

from __future__ import annotations

import copy
import os
import re

from state_io import read_json, atomic_write_json, file_lock

# State file (lives in the bot's working dir alongside risk_state.json etc.)
USER_CONFIG_FILE = "user_config.json"

# Every strategy the bot knows about, in display order. enabled_strategies and
# strategy_allocation_pct may only reference these names.
KNOWN_STRATEGIES = ["catalyst_long_call", "iv_rank", "hft_intraday", "pead", "bounce"]

# Strategies that place 0-DTE intraday DAY trades. These are the ones gated by
# the Pattern-Day-Trader rule (<$25k → max 3 day-trades / 5 business days), so
# the small/micro tiers disable them outright.
_DAY_STRATEGIES = {"hft_intraday"}


# ─── Tier presets ───────────────────────────────────────────────────────────────
# Each preset is a COMPLETE risk config. "standard" mirrors the live
# risk_manager.py constants exactly (the keep-it-as-it-is baseline); the smaller
# tiers progressively concentrate the book and drop strategies that don't fit a
# small account (HFT = PDT rule, catalyst = negative-EV per the bot's own
# backtests — see project memory).

TIER_PRESETS: dict[str, dict] = {
    # ≥ $25k — IDENTICAL to today's risk_manager defaults.
    "standard": {
        "risk_per_trade_pct":    0.03,
        "max_position_size_pct": 0.05,
        "daily_loss_limit_pct":  0.05,
        "max_swing_positions":   8,
        "max_day_positions":     5,
        # catalyst_long_call retired (negative-EV); hft_intraday PAUSED pending a
        # theta-honest backtest (its 0-DTE edge is unproven). Both still exit open
        # positions — they just can't open new ones. Mirrors risk_manager.
        "enabled_strategies":    ["iv_rank", "pead", "bounce"],
        "strategy_allocation_pct": {
            "iv_rank":            0.35,
            "hft_intraday":       0.25,
            "pead":               0.35,
            "bounce":             0.15,
        },
        "bear_regime_throttle":  True,
        # Round a too-small budget UP to 1 contract (small accounts only). OFF
        # here so large accounts keep strict %-sizing — identical to today.
        "min_one_contract":      False,
    },
    # $5k–$25k — no day-trading (PDT), fewer slots, lean on the validated swing
    # engines (iv_rank premium-selling + pead drift); catalyst (neg-EV) off.
    "small": {
        "risk_per_trade_pct":    0.05,
        "max_position_size_pct": 0.10,
        "daily_loss_limit_pct":  0.05,
        "max_swing_positions":   4,
        "max_day_positions":     0,
        "enabled_strategies":    ["iv_rank", "pead", "bounce"],
        "strategy_allocation_pct": {
            "iv_rank": 0.40,
            "pead":    0.35,
            "bounce":  0.25,
        },
        "bear_regime_throttle":  True,
        "min_one_contract":      True,    # let a small account actually fill a trade
    },
    # < $5k — concentrate hard: one or two engines, a couple of slots, larger
    # per-trade % (forced by contract granularity — one contract is a big chunk
    # of a tiny account). Day-trading and catalyst off.
    "micro": {
        "risk_per_trade_pct":    0.08,
        "max_position_size_pct": 0.15,
        "daily_loss_limit_pct":  0.05,
        "max_swing_positions":   2,
        "max_day_positions":     0,
        "enabled_strategies":    ["iv_rank", "pead"],
        "strategy_allocation_pct": {
            "iv_rank": 0.60,
            "pead":    0.40,
        },
        "bear_regime_throttle":  True,
        "min_one_contract":      True,
    },
}

# Equity floor (USD) at/above which each tier applies. Highest matching tier
# wins. Edited in one place so the thresholds stay consistent everywhere.
TIER_THRESHOLDS: list[tuple[float, str]] = [
    (25_000.0, "standard"),
    (5_000.0,  "small"),
    (0.0,      "micro"),
]

# Display order, smallest → largest.
TIER_ORDER = ["micro", "small", "standard"]


# ─── Safe bounds (clamped on every read AND write) ──────────────────────────────
# (min, max) for each scalar field. These are HARD safety rails — no dashboard
# input can ever push a value outside them. daily_loss_limit_pct's floor of 0.01
# is what makes the halt un-disable-able.
_BOUNDS: dict[str, tuple[float, float]] = {
    "risk_per_trade_pct":    (0.005, 0.10),
    "max_position_size_pct": (0.01,  0.25),
    "daily_loss_limit_pct":  (0.01,  0.10),
    "max_swing_positions":   (0, 20),
    "max_day_positions":     (0, 20),
}
_INT_FIELDS = {"max_swing_positions", "max_day_positions"}
# Per-strategy capital allocation fraction bound.
_ALLOC_BOUND = (0.0, 2.0)


# ─── Scanner-alert preferences ──────────────────────────────────────────────────
# These are GLOBAL user prefs, independent of account size — they don't belong in
# the tier presets. They control the scanner setup alerts in event_notifier:
#   enabled     master on/off for "fresh setup" pushes
#   min_score   only setups scoring >= this alert (watchlist tickers bypass it)
#   strategies  which strategy scanners may alert (watchlist tickers bypass this)
#   watchlist   tickers you always want to hear about — they alert on ANY setup,
#               below the score floor and across all strategies (master switch
#               still applies)
ALERT_DEFAULTS: dict = {
    "enabled":    True,
    "min_score":  60,
    "strategies": list(KNOWN_STRATEGIES),
    "watchlist":  [],
}
ALERT_MIN_SCORE_BOUND = (0, 100)
MAX_WATCHLIST = 50


# ─── Tier selection ─────────────────────────────────────────────────────────────

def select_tier(equity: float) -> str:
    """The tier an account of this equity falls into (auto-selection)."""
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        eq = 0.0
    for floor, name in TIER_THRESHOLDS:   # highest floor first
        if eq >= floor:
            return name
    return "micro"


# ─── Coercion + clamping helpers ────────────────────────────────────────────────

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _coerce_scalar(field: str, raw):
    """Coerce a JSON value to the field's type and clamp it, or None if invalid."""
    if field not in _BOUNDS:
        return None
    lo, hi = _BOUNDS[field]
    try:
        if field in _INT_FIELDS:
            val = int(float(raw))           # tolerate "4" and 4.0 from JSON
        else:
            val = float(raw)
    except (TypeError, ValueError):
        return None
    return _clamp(val, lo, hi)


def _clean_enabled(raw) -> list[str] | None:
    """Keep only known strategy names, de-duplicated, in canonical order."""
    if not isinstance(raw, list):
        return None
    keep = {s for s in raw if s in KNOWN_STRATEGIES}
    return [s for s in KNOWN_STRATEGIES if s in keep]


def _clean_allocation(raw) -> dict[str, float] | None:
    """Keep only known strategies and clamp each fraction."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for k, v in raw.items():
        if k not in KNOWN_STRATEGIES:
            continue
        try:
            out[k] = _clamp(float(v), *_ALLOC_BOUND)
        except (TypeError, ValueError):
            continue
    return out or None


def _clean_overrides(raw: dict) -> dict:
    """
    Sanitize a raw overrides dict from the dashboard into a safe subset.

    Drops unknown keys and anything that won't coerce. The result is safe to
    persist and to merge onto a tier preset.
    """
    if not isinstance(raw, dict):
        return {}
    clean: dict = {}
    for field in _BOUNDS:
        if field in raw:
            val = _coerce_scalar(field, raw[field])
            if val is not None:
                clean[field] = val
    if "enabled_strategies" in raw:
        en = _clean_enabled(raw["enabled_strategies"])
        if en is not None:
            clean["enabled_strategies"] = en
    if "strategy_allocation_pct" in raw:
        al = _clean_allocation(raw["strategy_allocation_pct"])
        if al is not None:
            clean["strategy_allocation_pct"] = al
    if "bear_regime_throttle" in raw:
        clean["bear_regime_throttle"] = bool(raw["bear_regime_throttle"])
    if "min_one_contract" in raw:
        clean["min_one_contract"] = bool(raw["min_one_contract"])
    return clean


def _clean_watchlist(raw) -> list[str]:
    """Sanitize a watchlist into uppercased, de-duplicated, plausible tickers.
    Accepts a list OR a comma/space-separated string (what the dashboard sends)."""
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().upper()
        if t and t.isalnum() and len(t) <= 6 and t not in out:
            out.append(t)
        if len(out) >= MAX_WATCHLIST:
            break
    return out


def _clean_alerts(raw) -> dict:
    """Sanitize/clamp a scanner-alert prefs dict, filling defaults for anything
    missing or invalid. Always returns a complete, safe dict."""
    out = {
        "enabled":    ALERT_DEFAULTS["enabled"],
        "min_score":  ALERT_DEFAULTS["min_score"],
        "strategies": list(KNOWN_STRATEGIES),
        "watchlist":  [],
    }
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "min_score" in raw:
        try:
            out["min_score"] = int(_clamp(float(raw["min_score"]), *ALERT_MIN_SCORE_BOUND))
        except (TypeError, ValueError):
            pass
    if "strategies" in raw:
        en = _clean_enabled(raw["strategies"])
        if en is not None:
            out["strategies"] = en
    if "watchlist" in raw:
        out["watchlist"] = _clean_watchlist(raw["watchlist"])
    return out


def _clamp_config(cfg: dict) -> dict:
    """Clamp a fully-merged config dict in place (defense-in-depth on read)."""
    for field in _BOUNDS:
        if field in cfg:
            val = _coerce_scalar(field, cfg[field])
            # Fall back to the standard preset if a preset value is somehow bad.
            cfg[field] = val if val is not None else TIER_PRESETS["standard"][field]
    cfg["enabled_strategies"] = _clean_enabled(cfg.get("enabled_strategies")) or []
    cfg["strategy_allocation_pct"] = _clean_allocation(cfg.get("strategy_allocation_pct")) or {}
    cfg["bear_regime_throttle"] = bool(cfg.get("bear_regime_throttle", True))
    cfg["min_one_contract"] = bool(cfg.get("min_one_contract", False))
    return cfg


# ─── Persistence (mtime-cached) ─────────────────────────────────────────────────

_cache: dict = {"mtime": None, "raw": None}


def load_user_config() -> dict:
    """
    Read the raw user override file: {"tier": ..., "overrides": {...}}.

    Returns {} when no file exists (→ pure auto-tier behaviour). Cached on the
    file's mtime so repeated calls in a single trade check don't re-read disk,
    while a dashboard save (which bumps mtime) is picked up immediately.
    """
    try:
        mtime = os.path.getmtime(USER_CONFIG_FILE)
    except OSError:
        mtime = None

    if mtime == _cache["mtime"] and _cache["raw"] is not None:
        return _cache["raw"]

    raw = read_json(USER_CONFIG_FILE, default={}) or {}
    if not isinstance(raw, dict):
        raw = {}
    _cache["mtime"] = mtime
    _cache["raw"] = raw
    return raw


def save_user_config(payload: dict) -> dict:
    """
    Validate, clamp, and persist a config payload from the dashboard.

    Accepts {"tier": "auto"|<tier>, "overrides": {...}, "alerts": {...}} and
    ignores everything else. MERGES with the existing file: a section absent from
    the payload is preserved from disk, so saving the tier never wipes the alert
    prefs (and vice versa). Returns the cleaned dict that was written.
    Cross-process locked + atomic so a reader mid-write never sees a partial file.
    """
    if not isinstance(payload, dict):
        payload = {}
    existing = load_user_config()

    tier = payload.get("tier", existing.get("tier", "auto"))
    if tier not in ("auto", *TIER_PRESETS):
        tier = "auto"

    overrides = payload["overrides"] if "overrides" in payload else existing.get("overrides", {})

    cleaned = {
        "tier":      tier,
        "overrides": _clean_overrides(overrides),
    }

    # Alerts: from the payload if present, else preserve what's on disk.
    if "alerts" in payload:
        cleaned["alerts"] = _clean_alerts(payload["alerts"])
    elif "alerts" in existing:
        cleaned["alerts"] = _clean_alerts(existing["alerts"])

    try:
        from utils import now_est
        cleaned["updated_at"] = now_est().isoformat(timespec="seconds")
    except Exception:
        pass

    with file_lock(USER_CONFIG_FILE):
        atomic_write_json(USER_CONFIG_FILE, cleaned)
    # Invalidate the cache so the next read reflects the new file immediately.
    _cache["mtime"] = None
    _cache["raw"] = None
    return cleaned


# ─── The one function everything else calls ─────────────────────────────────────

def effective_config(equity: float) -> dict:
    """
    Resolve the ACTIVE risk config for an account of this equity.

        1. Pick the tier — the user's pinned tier, else auto from equity.
        2. Start from that tier's preset.
        3. Merge the user's field overrides on top.
        4. Clamp every value to its safe bound.

    Always returns a complete, safe config — never raises. Includes metadata
    (`tier`, `tier_source`, `auto_tier`) so the dashboard can show what's active
    and why.
    """
    raw = load_user_config()

    pinned = raw.get("tier", "auto")
    auto_tier = select_tier(equity)
    tier = pinned if pinned in TIER_PRESETS else auto_tier

    cfg = copy.deepcopy(TIER_PRESETS.get(tier, TIER_PRESETS["standard"]))

    overrides = _clean_overrides(raw.get("overrides", {}))
    cfg.update(overrides)
    cfg = _clamp_config(cfg)

    cfg["tier"] = tier
    cfg["tier_source"] = "pinned" if pinned in TIER_PRESETS else "auto"
    cfg["auto_tier"] = auto_tier
    return cfg


def is_strategy_enabled(strategy: str | None, equity: float) -> bool:
    """True if `strategy` may open new trades at this equity (None → always)."""
    if not strategy:
        return True
    return strategy in effective_config(equity)["enabled_strategies"]


def alert_config() -> dict:
    """Active scanner-alert preferences (global; NOT account-size dependent).
    Always returns a complete, clamped dict — defaults when nothing is saved."""
    return _clean_alerts(load_user_config().get("alerts"))


# ─── CLI: inspect the resolved config for a given equity ────────────────────────

if __name__ == "__main__":
    import json
    import sys

    eq = float(sys.argv[1]) if len(sys.argv) > 1 else 100_000.0
    cfg = effective_config(eq)
    print(f"Equity ${eq:,.0f}  ->  tier '{cfg['tier']}' "
          f"({cfg['tier_source']}; auto would be '{cfg['auto_tier']}')\n")
    print(json.dumps(cfg, indent=2))
