import pandas as pd

from market_data import get_daily_bars
from utils import today_est
from state_io import read_json, atomic_write_json


def get_daily_data(ticker: str, period: str = "3mo") -> pd.DataFrame:
    """Daily OHLCV for liquidity filtering via Tradier."""
    days_map = {"3mo": 90, "1mo": 30, "6mo": 180, "1y": 365}
    days = days_map.get(period, 90)
    return get_daily_bars(ticker, days=days)


# ─── Daily liquidity cache ──────────────────────────────────────────────────────
# Liquidity metrics (20-day avg volume / dollar volume / price) are DAILY figures
# that don't meaningfully change within a trading session. The live scanners,
# however, call filter_universe every cycle (the HFT loop every 60s), which used
# to re-download daily history for every symbol on every cycle — dozens of
# identical API calls per minute, all day.
#
# We cache the raw metrics per symbol, keyed by the ET trading day, on disk so
# all three strategy processes share it. RAW metrics are cached (not the pass/
# fail verdict) so callers passing different thresholds still get correct results.
_LIQUIDITY_CACHE_FILE = "liquidity_cache.json"


def _load_liquidity_cache() -> dict:
    """Return today's cached {symbol: metrics}, or {} if missing/stale.

    A new ET trading day invalidates the whole cache (daily metrics roll over).
    """
    data = read_json(_LIQUIDITY_CACHE_FILE, {})
    if not isinstance(data, dict) or data.get("date") != today_est().isoformat():
        return {}
    metrics = data.get("metrics", {})
    return metrics if isinstance(metrics, dict) else {}


def _save_liquidity_cache(metrics: dict) -> None:
    """Persist the metrics map under today's ET date (atomic, cross-process safe)."""
    try:
        atomic_write_json(
            _LIQUIDITY_CACHE_FILE,
            {"date": today_est().isoformat(), "metrics": metrics},
        )
    except Exception as e:
        print(f"[Filter] Could not persist liquidity cache: {e}")


def calculate_liquidity_metrics(df: pd.DataFrame) -> dict:
    """
    Calculate the basic liquidity/tradability metrics we care about.

    Returns:
        {
            "last_close": float,
            "avg_volume_20": float,
            "avg_dollar_volume_20": float
        }
    """
    # Safety check in case the dataframe is too small
    if df.empty or len(df) < 20:
        return {
            "last_close": 0.0,
            "avg_volume_20": 0.0,
            "avg_dollar_volume_20": 0.0
        }

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)

    last_close = float(close.iloc[-1])

    # Average daily volume over the last 20 trading days
    avg_volume_20 = float(volume.tail(20).mean())

    # Dollar volume = price * shares traded
    # This helps filter out names that may have volume,
    # but still don't trade enough actual money.
    dollar_volume = close * volume
    avg_dollar_volume_20 = float(dollar_volume.tail(20).mean())

    return {
        "last_close": last_close,
        "avg_volume_20": avg_volume_20,
        "avg_dollar_volume_20": avg_dollar_volume_20
    }


def passes_liquidity_filter(
    metrics: dict,
    min_price: float = 5.0,
    min_avg_volume: float = 1_000_000,
    min_avg_dollar_volume: float = 20_000_000
) -> bool:
    """
    Decide whether a stock is liquid/tradable enough to keep.

    Default thresholds:
    - Price must be at least $5
    - Average daily volume must be at least 1M shares
    - Average daily dollar volume must be at least $20M
    """
    return (
        metrics["last_close"] >= min_price
        and metrics["avg_volume_20"] >= min_avg_volume
        and metrics["avg_dollar_volume_20"] >= min_avg_dollar_volume
    )


def filter_universe(
    symbols: list[str],
    min_price: float = 5.0,
    min_avg_volume: float = 1_000_000,
    min_avg_dollar_volume: float = 20_000_000,
    max_symbols: int | None = None,
    use_cache: bool = True,
) -> list[str]:
    """
    Filter a universe of symbols down to tradable/liquid names.

    This function:
    1. Looks up each symbol's daily liquidity metrics (cached per ET day) —
       downloading daily data only on a cache miss
    2. Keeps only symbols that meet our minimum standards

    Args:
        symbols: List of ticker symbols to test
        min_price: Minimum stock price
        min_avg_volume: Minimum average daily share volume
        min_avg_dollar_volume: Minimum average daily dollar volume
        max_symbols: Optional cap on how many filtered names to return
        use_cache: Reuse today's cached metrics instead of re-downloading
                   (set False to force a fresh fetch, e.g. in tests)

    Returns:
        A filtered list of symbols that pass the liquidity screen.
    """
    filtered: list[str] = []
    cache = _load_liquidity_cache() if use_cache else {}
    cache_hits = 0
    fetched = 0

    for ticker in symbols:
        try:
            metrics = cache.get(ticker) if use_cache else None

            if metrics is None:
                df = get_daily_data(ticker)
                if df.empty or len(df) < 20:
                    # Don't cache a no-data verdict — it may be a transient
                    # fetch failure; allow a retry on the next cycle.
                    continue
                metrics = calculate_liquidity_metrics(df)
                cache[ticker] = metrics
                fetched += 1
            else:
                cache_hits += 1

            if passes_liquidity_filter(
                metrics=metrics,
                min_price=min_price,
                min_avg_volume=min_avg_volume,
                min_avg_dollar_volume=min_avg_dollar_volume,
            ):
                filtered.append(ticker)

            # Optional early stop if you only want a subset for testing
            if max_symbols is not None and len(filtered) >= max_symbols:
                break

        except Exception as e:
            print(f"[Filter] Error with {ticker}: {e}")

    # Persist only when we actually fetched something new this call.
    if use_cache and fetched:
        _save_liquidity_cache(cache)

    print(
        f"[Filter] Kept {len(filtered)}/{len(symbols)} liquid "
        f"({cache_hits} cached, {fetched} fetched)."
    )
    return filtered