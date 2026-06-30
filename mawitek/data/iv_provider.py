"""
iv_provider.py — implied-volatility context for the scanners and dashboard.

An options bot that ignores IV is flying half-blind: the SAME directional setup is
a very different trade when options are cheap vs expensive. This module surfaces,
per ticker, whether volatility is rich or cheap:

  • ATM IV       — current at-the-money implied vol (from the Tradier/ORATS chain)
  • IV/HV ratio  — implied vs 20-day realized vol; >1.3 = options pricing in more
                   move than the stock has delivered (rich), <0.9 = cheap.
                   Available immediately (no history needed).
  • IV Rank/%ile — where current IV sits in its own trailing range. True IV rank
                   needs historical IV, which free data doesn't give — so we
                   ACCUMULATE it ourselves (iv_history.json, one reading/ticker/
                   day) and report it once there's enough history (else None).
  • regime       — "cheap" / "normal" / "rich" summary.

This is INFORMATIONAL (shown on setups + dashboard) — it does not gate trades.
Gating on IV (e.g. only buy premium when cheap, only sell when rich) would be a
trade-decision change and should be backtested first, like every other signal here.

Live reads (atm_iv / get_hv / iv_context) hit the broker; results are cached per
ET day in iv_cache.json so a name is priced at most once a day across the bot.
The pure helpers (compute_hv / iv_rank_from_history / classify_iv) are unit-tested.
"""

import datetime
import math

from mawitek.infra.state_io import read_json, atomic_write_json
from mawitek.infra.utils import today_est

IV_CACHE_FILE   = "iv_cache.json"
IV_HISTORY_FILE = "iv_history.json"

HV_WINDOW          = 20      # trading days for realized vol
IV_HISTORY_MAX     = 252     # ~1 trading year of daily IV readings per ticker
MIN_IV_HISTORY     = 10      # need at least this many readings before an IV rank
ATM_TARGET_DTE     = 30      # read IV from the ~monthly expiry (stable, not 0-DTE)
ATM_MIN_DTE        = 7       # ignore near-expiry chains (IV noisy into expiration)

# Regime thresholds (prefer IV rank when available, else the IV/HV ratio).
RANK_RICH, RANK_CHEAP   = 60.0, 25.0
RATIO_RICH, RATIO_CHEAP = 1.30, 0.90


# ─── Pure helpers (unit-tested) ─────────────────────────────────────────────────

def compute_hv(closes: list[float], window: int = HV_WINDOW) -> float | None:
    """Annualized realized volatility from a daily close series. None if too short."""
    closes = [float(c) for c in closes if c]
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))][-window:]
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


def iv_rank_from_history(current_iv: float, history: list[float]) -> float | None:
    """IV rank 0–100 = where current IV sits in its trailing [min,max]. None if thin."""
    vals = [float(v) for v in history if v]
    if len(vals) < MIN_IV_HISTORY:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return None
    return round((current_iv - lo) / (hi - lo) * 100, 1)


def iv_percentile_from_history(current_iv: float, history: list[float]) -> float | None:
    """% of trailing readings at or below current IV. None if thin."""
    vals = [float(v) for v in history if v]
    if len(vals) < MIN_IV_HISTORY:
        return None
    below = sum(1 for v in vals if v <= current_iv)
    return round(below / len(vals) * 100, 1)


def classify_iv(iv_hv_ratio: float | None, iv_rank: float | None) -> str:
    """'cheap' / 'normal' / 'rich' — IV rank preferred, IV/HV ratio as the fallback."""
    if iv_rank is not None:
        return "rich" if iv_rank >= RANK_RICH else "cheap" if iv_rank <= RANK_CHEAP else "normal"
    if iv_hv_ratio is not None:
        return "rich" if iv_hv_ratio >= RATIO_RICH else "cheap" if iv_hv_ratio <= RATIO_CHEAP else "normal"
    return "n/a"


# ─── IV history (accumulated so IV rank can exist at all) ───────────────────────

def _load_history() -> dict:
    data = read_json(IV_HISTORY_FILE, {})
    return data if isinstance(data, dict) else {}


def record_iv(ticker: str, iv: float) -> list[float]:
    """
    Append today's IV reading for `ticker` (one per ET day — re-reads update it),
    capped at IV_HISTORY_MAX. Returns the ticker's full IV series (oldest→newest).
    """
    hist = _load_history()
    day = today_est().isoformat()
    rows = [r for r in hist.get(ticker, []) if isinstance(r, dict) and r.get("date") != day]
    rows.append({"date": day, "iv": round(float(iv), 4)})
    rows = rows[-IV_HISTORY_MAX:]
    hist[ticker] = rows
    try:
        atomic_write_json(IV_HISTORY_FILE, hist)
    except Exception as e:
        print(f"[IV] Could not persist IV history: {e}")
    return [r["iv"] for r in rows]


# ─── Day cache ──────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    data = read_json(IV_CACHE_FILE, {})
    if not isinstance(data, dict) or data.get("date") != today_est().isoformat():
        return {}
    ctx = data.get("ctx", {})
    return ctx if isinstance(ctx, dict) else {}


def _save_cache_entry(ticker: str, ctx: dict) -> None:
    data = read_json(IV_CACHE_FILE, {})
    if not isinstance(data, dict) or data.get("date") != today_est().isoformat():
        data = {"date": today_est().isoformat(), "ctx": {}}
    data.setdefault("ctx", {})[ticker] = ctx
    try:
        atomic_write_json(IV_CACHE_FILE, data)
    except Exception as e:
        print(f"[IV] Could not persist IV cache: {e}")


# ─── Live reads (broker) ────────────────────────────────────────────────────────

def atm_iv(ticker: str, target_dte: int = ATM_TARGET_DTE) -> float | None:
    """Current at-the-money implied vol (fraction, e.g. 0.45) from the ~monthly chain."""
    from mawitek.data.tradier_client import get_quote, get_options_expirations, get_options_chain

    spot = get_quote(ticker)
    if spot <= 0:
        return None
    exps = get_options_expirations(ticker)
    if not exps:
        return None

    today = today_est()
    cand = []
    for e in exps:
        try:
            dte = (datetime.date.fromisoformat(e) - today).days
        except Exception:
            continue
        if dte >= ATM_MIN_DTE:
            cand.append((abs(dte - target_dte), e))
    if not cand:
        return None
    exp = min(cand)[1]

    calls = [c for c in get_options_chain(ticker, exp)
             if c.get("option_type") == "call" and (c.get("greeks") or {}).get("mid_iv")]
    if not calls:
        return None
    atm = min(calls, key=lambda c: abs(float(c.get("strike", 0) or 0) - spot))
    iv = float((atm.get("greeks") or {}).get("mid_iv", 0) or 0)
    return iv if iv > 0 else None


def get_hv(ticker: str) -> float | None:
    """Annualized 20-day realized vol from Tradier daily bars (None on no data)."""
    try:
        from mawitek.data.market_data import get_daily_bars
        df = get_daily_bars(ticker, days=HV_WINDOW * 2 + 5)
        if df is None or df.empty or "Close" not in df:
            return None
        return compute_hv([float(x) for x in df["Close"].tolist()])
    except Exception:
        return None


def iv_context(ticker: str, use_cache: bool = True) -> dict | None:
    """
    Full IV context for a ticker (day-cached). Returns:
        {atm_iv, hv, iv_hv_ratio, iv_rank, iv_percentile, regime}
    with IV values as PERCENT (45.0 = 45%). None if IV can't be read (e.g. no
    options / MOCK_MODE).
    """
    if use_cache:
        cached = _load_cache().get(ticker)
        if cached:
            return cached

    iv = atm_iv(ticker)
    if iv is None:
        return None

    hv = get_hv(ticker)
    ratio = round(iv / hv, 2) if hv and hv > 0 else None
    series = record_iv(ticker, iv)
    rank = iv_rank_from_history(iv, series)
    pctl = iv_percentile_from_history(iv, series)

    ctx = {
        "atm_iv":        round(iv * 100, 1),
        "hv":            round(hv * 100, 1) if hv else None,
        "iv_hv_ratio":   ratio,
        "iv_rank":       rank,
        "iv_percentile": pctl,
        "regime":        classify_iv(ratio, rank),
    }
    if use_cache:
        _save_cache_entry(ticker, ctx)
    return ctx
