from utils import percent_change
from config import STOP_LOSS, TAKE_PROFIT_MIN

positions = {}

def handle_trade(ticker, signal, price):
    if signal == "BUY" and ticker not in positions:
        positions[ticker] = price
        print(f"[BUY] {ticker} at {price}")

    elif ticker in positions:
        entry = positions[ticker]
        change = percent_change(entry, price)

        if change <= -STOP_LOSS:
            print(f"[STOP LOSS] {ticker} at {price}")
            del positions[ticker]

        elif change >= TAKE_PROFIT_MIN:
            print(f"[TAKE PROFIT] {ticker} at {price}")
            del positions[ticker]