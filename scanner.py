import yfinance as yf
import pandas as pd


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    yfinance sometimes returns MultiIndex columns.
    This flattens them into normal columns.
    """
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    return df


def get_data(ticker: str, interval: str = "1h", period: str = "5d") -> pd.DataFrame:
    """
    Download intraday data for signal generation.
    """
    df = yf.download(
        tickers=ticker,
        interval=interval,
        period=period,
        auto_adjust=False,
        progress=False,
        threads=False
    )

    df = flatten_columns(df)
    df.dropna(inplace=True)
    return df