import pandas as pd
import ta

def to_series(col):
    if isinstance(col, pd.DataFrame):
        return col.iloc[:, 0]
    return col

def generate_signal(df, ticker=""):
    close = to_series(df["Close"]).astype(float)
    volume = to_series(df["Volume"]).astype(float)

    df = df.copy()
    df["rsi"] = ta.momentum.RSIIndicator(close=close, window=14).rsi()

    macd = ta.trend.MACD(close=close)
    df["macd"] = macd.macd()
    df["signal"] = macd.macd_signal()

    df["vol_avg"] = volume.rolling(20).mean()

    if len(df) < 35:
        return "HOLD"

    rsi = float(df["rsi"].iloc[-1])

    macd_now = float(df["macd"].iloc[-1])
    macd_prev = float(df["macd"].iloc[-2])

    signal_now = float(df["signal"].iloc[-1])
    signal_prev = float(df["signal"].iloc[-2])

    vol_now = float(volume.iloc[-1])
    avg_vol = float(df["vol_avg"].iloc[-1])

    bullish_cross = macd_prev <= signal_prev and macd_now > signal_now
    bearish_cross = macd_prev >= signal_prev and macd_now < signal_now

    high_volume = vol_now > avg_vol * 1.1

    print(
        f"{ticker} | RSI={rsi:.2f} | MACD={macd_now:.4f} | SIGNAL={signal_now:.4f} | "
        f"BULL_CROSS={bullish_cross} | BEAR_CROSS={bearish_cross} | "
        f"VOL={vol_now:.0f} | AVG_VOL={avg_vol:.0f}"
    )

    if bullish_cross and rsi < 45 and high_volume:
        return "BUY"

    if bearish_cross and rsi > 55 and high_volume:
        return "SELL"

    return "HOLD"