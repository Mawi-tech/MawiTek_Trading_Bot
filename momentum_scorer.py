"""
momentum_scorer.py

Scores a ticker's price and volume momentum on a 0-100 scale.
Used to rank setups by strength before entering a call position.

Combines:
- Rate of change (ROC) over multiple timeframes
- Volume surge vs 20-day average
- Distance from 52-week high (proximity = momentum confirmation)
- RSI trend (rising RSI in healthy range)
"""

import yfinance as yf
import pandas as pd
import ta


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    return df


def get_daily_data(ticker: str, period: str = "1y") -> pd.DataFrame:
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


def score_momentum(ticker: str) -> dict:
    """
    Score a ticker's momentum on a 0-100 scale.

    Scoring breakdown (total 100 pts):
    - Volume surge vs 20d avg:     25 pts
    - 5-day price ROC:             20 pts
    - 10-day price ROC:            15 pts
    - RSI trend (rising + healthy):20 pts
    - Proximity to 52-week high:   20 pts

    Returns dict with total score and component breakdown.
    """
    try:
        df = get_daily_data(ticker)

        if df.empty or len(df) < 60:
            return _empty_score(ticker, reason="Not enough data")

        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)

        score = 0
        components = {}

        # --- Volume surge (25 pts) ---
        avg_vol_20 = float(volume.tail(20).mean())
        last_vol = float(volume.iloc[-1])
        vol_ratio = last_vol / avg_vol_20 if avg_vol_20 > 0 else 0

        if vol_ratio >= 3.0:
            vol_score = 25
        elif vol_ratio >= 2.0:
            vol_score = 20
        elif vol_ratio >= 1.5:
            vol_score = 15
        elif vol_ratio >= 1.2:
            vol_score = 10
        else:
            vol_score = 0

        score += vol_score
        components["volume_surge"] = round(vol_ratio, 2)
        components["volume_score"] = vol_score

        # --- 5-day ROC (20 pts) ---
        roc_5 = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100)
        if roc_5 >= 5:
            roc5_score = 20
        elif roc_5 >= 3:
            roc5_score = 15
        elif roc_5 >= 1.5:
            roc5_score = 10
        elif roc_5 >= 0:
            roc5_score = 5
        else:
            roc5_score = 0

        score += roc5_score
        components["roc_5d"] = round(roc_5, 2)
        components["roc5_score"] = roc5_score

        # --- 10-day ROC (15 pts) ---
        roc_10 = float((close.iloc[-1] - close.iloc[-11]) / close.iloc[-11] * 100)
        if roc_10 >= 8:
            roc10_score = 15
        elif roc_10 >= 5:
            roc10_score = 10
        elif roc_10 >= 2:
            roc10_score = 7
        elif roc_10 >= 0:
            roc10_score = 3
        else:
            roc10_score = 0

        score += roc10_score
        components["roc_10d"] = round(roc_10, 2)
        components["roc10_score"] = roc10_score

        # --- RSI trend (20 pts) ---
        rsi_series = ta.momentum.RSIIndicator(close=close, window=14).rsi()
        rsi_now = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-5])  # vs 5 days ago
        rsi_rising = rsi_now > rsi_prev
        rsi_healthy = 45 <= rsi_now <= 70  # Not overbought, not weak

        if rsi_rising and rsi_healthy:
            rsi_score = 20
        elif rsi_rising or rsi_healthy:
            rsi_score = 10
        else:
            rsi_score = 0

        score += rsi_score
        components["rsi"] = round(rsi_now, 2)
        components["rsi_rising"] = rsi_rising
        components["rsi_score"] = rsi_score

        # --- 52-week high proximity (20 pts) ---
        high_52w = float(close.tail(252).max())
        last_close = float(close.iloc[-1])
        pct_from_high = (last_close / high_52w) * 100

        if pct_from_high >= 95:
            high_score = 20
        elif pct_from_high >= 90:
            high_score = 15
        elif pct_from_high >= 80:
            high_score = 8
        else:
            high_score = 0

        score += high_score
        components["pct_from_52w_high"] = round(pct_from_high, 2)
        components["high_proximity_score"] = high_score

        print(
            f"[Momentum] {ticker} | Score: {score}/100 | "
            f"Vol: {vol_ratio:.1f}x | ROC5: {roc_5:.1f}% | "
            f"RSI: {rsi_now:.1f} | 52W%: {pct_from_high:.1f}%"
        )

        return {
            "ticker": ticker,
            "score": score,
            "components": components,
            "error": None
        }

    except Exception as e:
        print(f"[Momentum] Error scoring {ticker}: {e}")
        return _empty_score(ticker, reason=str(e))


def _empty_score(ticker: str, reason: str = "") -> dict:
    return {
        "ticker": ticker,
        "score": 0,
        "components": {},
        "error": reason
    }


def rank_by_momentum(symbols: list[str], min_score: int = 40) -> list[dict]:
    """
    Score and rank a list of symbols by momentum.
    Only returns tickers above min_score threshold.
    """
    results = []

    for ticker in symbols:
        result = score_momentum(ticker)
        if result["score"] >= min_score:
            results.append(result)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
