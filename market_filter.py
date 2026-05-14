import pandas as pd
import yfinance as yf


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    yfinance sometimes returns MultiIndex columns.
    This flattens them into normal single-level columns.
    """
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    return df


def get_daily_data(ticker: str, period: str = "3mo") -> pd.DataFrame:
    """
    Download daily data for a stock.

    We use daily candles here because liquidity filters like
    average volume and dollar volume make more sense on daily data.
    """
    df = yf.download(
        tickers=ticker,
        interval="1d",
        period=period,
        auto_adjust=False,
        progress=False,
        threads=False
    )

    df = flatten_columns(df)
    df.dropna(inplace=True)
    return df


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
    max_symbols: int | None = None
) -> list[str]:
    """
    Filter a universe of symbols down to tradable/liquid names.

    This function:
    1. Downloads daily data for each symbol
    2. Computes liquidity metrics
    3. Keeps only symbols that meet our minimum standards

    Args:
        symbols: List of ticker symbols to test
        min_price: Minimum stock price
        min_avg_volume: Minimum average daily share volume
        min_avg_dollar_volume: Minimum average daily dollar volume
        max_symbols: Optional cap on how many filtered names to return

    Returns:
        A filtered list of symbols that pass the liquidity screen.
    """
    filtered = []

    print(f"[Filter] Checking {len(symbols)} symbols for liquidity...")

    for ticker in symbols:
        try:
            df = get_daily_data(ticker)

            if df.empty or len(df) < 20:
                print(f"[Filter] {ticker}: skipped (not enough daily data)")
                continue

            metrics = calculate_liquidity_metrics(df)

            passes = passes_liquidity_filter(
                metrics=metrics,
                min_price=min_price,
                min_avg_volume=min_avg_volume,
                min_avg_dollar_volume=min_avg_dollar_volume
            )

            print(
                f"[Filter] {ticker} | "
                f"Close={metrics['last_close']:.2f} | "
                f"AvgVol20={metrics['avg_volume_20']:.0f} | "
                f"AvgDollarVol20={metrics['avg_dollar_volume_20']:.0f} | "
                f"Pass={passes}"
            )

            if passes:
                filtered.append(ticker)

            # Optional early stop if you only want a subset for testing
            if max_symbols is not None and len(filtered) >= max_symbols:
                break

        except Exception as e:
            print(f"[Filter] Error with {ticker}: {e}")

    print(f"[Filter] Kept {len(filtered)} of {len(symbols)} symbols.")
    return filtered