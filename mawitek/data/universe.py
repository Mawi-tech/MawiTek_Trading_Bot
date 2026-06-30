import os
import json
import random
import pandas as pd

# Fallback universe so the bot still works if CSV/API sources fail
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
    "NFLX", "SMCI", "MU", "AVGO", "PLTR", "INTC", "QCOM", "ADBE",
    "CRM", "PYPL", "UBER", "SHOP", "PANW", "CRWD", "SNOW", "MRVL",
    "ARM", "ANET", "AMAT", "KLAC", "LRCX", "MSTR", "COIN", "NET",
    "DDOG", "ZS", "TTD", "RBLX", "HOOD", "ABNB", "COST", "WMT",
    "JPM", "BAC", "GS", "XOM", "CVX", "LLY", "UNH", "JNJ", "PFE"
]

# Persistent state file for rotation offset.
# Lives next to this module — tiny JSON, safe to delete.
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".universe_state.json")

# Universe CSV filenames. The full-market list (update_universe.py) and the
# pre-screened liquid list (screen_universe.py) are preferred when present; the
# S&P 500 file is the back-compat fallback.
MARKET_UNIVERSE_CSV = "market_universe.csv"   # full US market, ~10k raw symbols
LIQUID_UNIVERSE_CSV = "liquid_universe.csv"   # screened: illiquid + micro-caps removed
SP500_CSV = "sp500.csv"


def market_csv() -> str:
    """
    Absolute path to the broadest available RAW universe CSV (the screener's input).

    Prefers the full-market list (market_universe.csv) when it exists, then the
    S&P 500 list. Returns the bare "sp500.csv" name if neither exists yet (so a
    relative-path load still works from the bot's working directory, and
    load_universe falls back to the built-in list if even that is missing).

    Generate the files with `python update_universe.py`.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for name in (MARKET_UNIVERSE_CSV, SP500_CSV):
        path = os.path.join(here, name)
        if os.path.exists(path):
            return path
    return SP500_CSV


def scan_csv() -> str:
    """
    Absolute path to the universe the LIVE scanners should rotate through.

    Prefers the pre-screened LIQUID universe (liquid_universe.csv — illiquid
    names and micro-caps removed, so every scan cycle is spent on tradable
    names), then the full market, then the S&P 500. Build/refresh the liquid
    list with `python screen_universe.py` (weekly). The per-scan liquidity
    filter still re-validates each name live, so a slightly stale liquid list
    is safe.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for name in (LIQUID_UNIVERSE_CSV, MARKET_UNIVERSE_CSV, SP500_CSV):
        path = os.path.join(here, name)
        if os.path.exists(path):
            return path
    return SP500_CSV


# ─── Symbol cleanup helpers ────────────────────────────────────────────────────

def clean_symbol(symbol: str) -> str | None:
    """
    Normalize stock symbols and reject invalid values.
    """
    if symbol is None:
        return None

    symbol = str(symbol).strip().upper()

    if not symbol:
        return None

    # Common cleanup for downloaded/index symbols
    symbol = symbol.replace(".", "-")

    # Ignore obvious bad rows
    if symbol in {"NAN", "NONE", "SYMBOL"}:
        return None

    return symbol


def dedupe_symbols(symbols: list[str]) -> list[str]:
    """
    Remove duplicates while preserving order.
    """
    seen = set()
    result = []

    for sym in symbols:
        cleaned = clean_symbol(sym)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)

    return result


def load_symbols_from_csv(filepath: str, column_name: str = "Symbol") -> list[str]:
    """
    Load symbols from a CSV file.
    Expected format:
        Symbol
        AAPL
        MSFT
        NVDA
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"CSV not found: {filepath}")

    df = pd.read_csv(filepath)

    if column_name not in df.columns:
        raise ValueError(f"Column '{column_name}' not found in {filepath}")

    symbols = df[column_name].dropna().astype(str).tolist()
    return dedupe_symbols(symbols)


# ─── Rotation state (persistent offset) ────────────────────────────────────────

def _load_state() -> dict:
    """Read the rotation offset state file. Returns empty dict on any failure."""
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict) -> None:
    """Write the rotation offset state file. Failures are non-fatal."""
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"[Universe] Warning: could not save rotation state: {e}")


def reset_rotation_state() -> None:
    """Clear the persistent rotation offset (forces next call to start at 0)."""
    if os.path.exists(_STATE_FILE):
        try:
            os.remove(_STATE_FILE)
            print("[Universe] Rotation state reset.")
        except OSError as e:
            print(f"[Universe] Could not reset rotation state: {e}")


# ─── Slicing modes ─────────────────────────────────────────────────────────────

def _slice_universe(
    symbols: list[str],
    limit: int | None,
    mode: str,
    state_key: str
) -> list[str]:
    """
    Apply a slicing mode to the symbol list.

    Modes:
        "head"   — first N symbols (legacy behavior, alphabetical blind spot)
        "random" — N random symbols, fresh sample every call
        "rotate" — N symbols starting from a persistent rolling offset; wraps around
    """
    if limit is None or limit >= len(symbols):
        return symbols

    if mode == "head":
        return symbols[:limit]

    if mode == "random":
        return random.sample(symbols, limit)

    if mode == "rotate":
        state = _load_state()
        offset = int(state.get(state_key, 0)) % len(symbols)

        # Take `limit` symbols starting at offset, wrapping around the end of the list
        if offset + limit <= len(symbols):
            window = symbols[offset:offset + limit]
        else:
            window = symbols[offset:] + symbols[: (offset + limit) - len(symbols)]

        # Advance the offset for next call
        state[state_key] = (offset + limit) % len(symbols)
        _save_state(state)

        print(f"[Universe] Rotation window: offset={offset} -> next={state[state_key]} "
              f"(scanning {window[0]}..{window[-1]})")
        return window

    raise ValueError(f"Unknown slicing mode: {mode!r}. Use 'head', 'random', or 'rotate'.")


# ─── Public API ────────────────────────────────────────────────────────────────

def get_default_universe(
    limit: int | None = None,
    mode: str = "rotate"
) -> list[str]:
    """
    Return the built-in fallback universe, optionally limited via a slicing mode.
    """
    symbols = dedupe_symbols(DEFAULT_UNIVERSE)
    return _slice_universe(symbols, limit, mode, state_key="default")


def load_universe(
    csv_path: str | None = None,
    csv_column: str = "Symbol",
    limit: int | None = None,
    mode: str = "rotate",
    rotation_key: str | None = None
) -> list[str]:
    """
    Load the market universe from CSV if available, otherwise the built-in fallback.

    Args:
        csv_path:     Optional path to a CSV with a Symbol column.
        csv_column:   Column name in the CSV (default "Symbol").
        limit:        Maximum number of symbols to return. If None, returns the full list.
        mode:         Slicing mode when `limit` is set:
                        "head"   — first N (alphabetical blind spot — back-compat only)
                        "random" — N random symbols each call
                        "rotate" — rolling window with persistent offset (DEFAULT)
        rotation_key: Persistent-offset bucket for "rotate" mode. Each distinct key
                      keeps its OWN rolling offset, so separate scanners (catalyst,
                      hft, pead, bounce) each sweep the full universe independently
                      instead of sharing one pointer (where the fast scanner would
                      consume most of the advancement and starve the slow ones).
                      Defaults to the CSV/built-in bucket for back-compat.

    Priority:
        1. CSV file (if provided and loadable)
        2. Built-in fallback list
    """
    symbols: list[str] = []
    state_key = rotation_key or ("csv" if csv_path else "default")

    if csv_path:
        try:
            symbols = load_symbols_from_csv(csv_path, csv_column)
            print(f"[Universe] Loaded {len(symbols)} symbols from CSV: {csv_path}")
        except Exception as e:
            print(f"[Universe] CSV load failed: {e}")
            print("[Universe] Falling back to default universe.")

    if not symbols:
        symbols = dedupe_symbols(DEFAULT_UNIVERSE)
        if not rotation_key:
            state_key = "default"
        print(f"[Universe] Loaded {len(symbols)} symbols from built-in fallback list.")

    sliced = _slice_universe(symbols, limit, mode, state_key=state_key)

    if limit is not None and len(sliced) < len(symbols):
        print(f"[Universe] Limited universe to {len(sliced)} symbols (mode={mode}).")

    return sliced
