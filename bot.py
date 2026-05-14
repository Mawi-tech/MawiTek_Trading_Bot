import time
import pandas as pd

from universe import load_universe
from scanner import get_data
from strategy import generate_signal
from trader import handle_trade
from market_filter import filter_universe


def to_scalar_price(col):
    """
    Convert a pandas column or DataFrame slice into one float price.
    This protects us from yfinance returning a 2D shape.
    """
    if isinstance(col, pd.DataFrame):
        return float(col.iloc[-1, 0])
    return float(col.iloc[-1])


def build_tradable_universe() -> list[str]:
    """
    Load the broad universe, then filter it down to liquid/tradable stocks.

    We use the CSV if available.
    If it doesn't exist, the universe loader falls back automatically.
    """
    raw_symbols = load_universe(csv_path="sp500.csv", limit=100)

    tradable_symbols = filter_universe(
        symbols=raw_symbols,
        min_price=5.0,
        min_avg_volume=1_000_000,
        min_avg_dollar_volume=20_000_000
    )

    return tradable_symbols


def run():
    """
    Main bot loop.

    Step 1: Build the tradable universe (rotates each cycle — see universe.py)
    Step 2: Pull 1H data for each stock
    Step 3: Generate signal
    Step 4: Pass signal to trade manager
    """
    while True:
        # Rebuild the universe each cycle so rotation in universe.load_universe()
        # actually advances. Otherwise we'd freeze on whichever 100-ticker window
        # we happened to land on at startup.
        tickers = build_tradable_universe()

        if not tickers:
            print("[Bot] No tradable symbols this cycle. Sleeping and retrying...\n")
            time.sleep(300)
            continue

        print(f"\n[Bot] Scanning {len(tickers)} filtered symbols...\n")

        for ticker in tickers:
            try:
                df = get_data(ticker)

                if df.empty or len(df) < 35:
                    print(f"{ticker}: not enough intraday data")
                    continue

                signal = generate_signal(df, ticker)
                price = to_scalar_price(df["Close"])

                print(f"{ticker}: {signal} @ {price:.2f}")
                handle_trade(ticker, signal, price)

            except Exception as e:
                print(f"Error with {ticker}: {e}")

        print("\n[Bot] Scan complete. Sleeping for 300 seconds...\n")
        time.sleep(300)


if __name__ == "__main__":
    run()