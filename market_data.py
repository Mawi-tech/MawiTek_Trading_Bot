"""
market_data.py

Tradier-backed market data module for the live bot.
Replaces yfinance for all real-time / intraday data needs.

Why not yfinance for live trading?
  - Yahoo Finance ToS prohibits commercial use
  - yfinance has no SLA — Yahoo can cut access with zero warning
  - Tradier (your broker) already provides quotes, history, options, and news
    at no extra cost once you have a production account

Functions
---------
  get_daily_bars(ticker, days)       → pd.DataFrame  OHLCV daily candles
  get_intraday_bars(ticker, interval, days) → pd.DataFrame  intraday candles
  get_news(ticker, max_articles)     → list[dict]    news headlines
  chain_to_df(chain, option_type)    → pd.DataFrame  options chain as DataFrame

MOCK_MODE
  When no TRADIER_API_KEY is configured (i.e. MOCK_MODE is True in
  tradier_client.py), every function returns an empty DataFrame / empty list
  so scanners degrade gracefully instead of crashing.

Backtests
  backtest_*.py files still use yfinance — they need multiple years of
  history that Tradier's free tier doesn't carry.  Don't change those.
"""

import datetime

import pandas as pd
import requests

from tradier_client import BASE_URL, HEADERS, MOCK_MODE
from logger import get_logger

log = get_logger("market_data")

# ── Interval mapping: yfinance notation → Tradier notation ────────────────────
_INTERVAL_MAP = {
    "1m":  "1min",
    "2m":  "1min",   # Tradier has no 2m; round down to 1m
    "5m":  "5min",
    "15m": "15min",
    "1min":  "1min",
    "5min":  "5min",
    "15min": "15min",
}


# ─── Daily OHLCV ──────────────────────────────────────────────────────────────

def get_daily_bars(ticker: str, days: int = 252) -> pd.DataFrame:
    """
    Daily OHLCV bars from Tradier /markets/history.

    Replaces:
        yf.download(tickers=ticker, interval="1d", period="1y")

    Args:
        ticker: Stock symbol
        days:   Calendar days of history to fetch (252 ≈ 1 trading year)

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume]
        indexed by date (DatetimeIndex).  Empty DataFrame on failure.
    """
    if MOCK_MODE:
        return pd.DataFrame()

    end   = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    url    = f"{BASE_URL}/markets/history"
    params = {
        "symbol":   ticker,
        "interval": "daily",
        "start":    start.isoformat(),
        "end":      end.isoformat(),
    }

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        history = data.get("history") or {}
        days_data = history.get("day", [])

        if not days_data:
            log.debug("No daily data for %s", ticker)
            return pd.DataFrame()

        # Tradier returns a single dict when there's only 1 bar
        if isinstance(days_data, dict):
            days_data = [days_data]

        df = pd.DataFrame(days_data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        # Rename to standard OHLCV column names (match yfinance convention)
        df = df.rename(columns={
            "open":   "Open",
            "high":   "High",
            "low":    "Low",
            "close":  "Close",
            "volume": "Volume",
        })

        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].apply(pd.to_numeric, errors="coerce").dropna()
        return df

    except Exception as e:
        log.warning("get_daily_bars(%s): %s", ticker, e)
        return pd.DataFrame()


# ─── Intraday OHLCV ───────────────────────────────────────────────────────────

def get_intraday_bars(
    ticker:   str,
    interval: str = "5min",
    days:     int = 1,
) -> pd.DataFrame:
    """
    Intraday OHLCV bars from Tradier /markets/timesales.

    Replaces:
        yf.download(tickers=ticker, interval="5m", period="1d")

    Args:
        ticker:   Stock symbol
        interval: Bar size — "1min", "5min", or "15min"
                  Also accepts yfinance notation: "1m", "5m", "15m"
        days:     How many trading days back to fetch (1 = today only)

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume]
        indexed by datetime (timezone-aware, US/Eastern).
        Empty DataFrame on failure or outside market hours.
    """
    if MOCK_MODE:
        return pd.DataFrame()

    tradier_interval = _INTERVAL_MAP.get(interval, interval)

    # Build date range — always start at market open, end at close
    end_dt   = datetime.datetime.now()
    start_dt = end_dt - datetime.timedelta(days=max(days, 1))

    # Format: "YYYY-MM-DD HH:MM"
    start_str = start_dt.strftime("%Y-%m-%d 09:30")
    end_str   = end_dt.strftime("%Y-%m-%d 16:00")

    url    = f"{BASE_URL}/markets/timesales"
    params = {
        "symbol":         ticker,
        "interval":       tradier_interval,
        "start":          start_str,
        "end":            end_str,
        "session_filter": "open",   # market hours only
    }

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        series = data.get("series") or {}
        bars   = series.get("data", [])

        if not bars:
            log.debug("No intraday data for %s (%s)", ticker, interval)
            return pd.DataFrame()

        if isinstance(bars, dict):
            bars = [bars]

        df = pd.DataFrame(bars)

        # Tradier timesales uses "time" key for the bar timestamp
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time").sort_index()

        # Rename to standard OHLCV — timesales uses lowercase
        df = df.rename(columns={
            "open":   "Open",
            "high":   "High",
            "low":    "Low",
            "close":  "Close",
            "volume": "Volume",
        })

        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[cols].apply(pd.to_numeric, errors="coerce").dropna()
        return df

    except Exception as e:
        log.warning("get_intraday_bars(%s, %s): %s", ticker, interval, e)
        return pd.DataFrame()


# ─── News ─────────────────────────────────────────────────────────────────────

def get_news(ticker: str, max_articles: int = 10) -> list[dict]:
    """
    Recent news headlines from Tradier /markets/news.

    Replaces:
        yf.Ticker(ticker).news

    Returns a list of dicts normalised to the same keys news_catalyst.py
    expects:
        title             str   headline text
        providerPublishTime int  Unix timestamp
        link              str   article URL
        publisher         str   source name

    Empty list on failure or MOCK_MODE.
    """
    if MOCK_MODE:
        return []

    url    = f"{BASE_URL}/markets/news"
    params = {
        "symbols":      ticker,
        "maxheadlines": max_articles,
    }

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)

        # Tradier sandbox returns 404 for the news endpoint — not available
        # in sandbox mode.  Silently return empty rather than spamming warnings.
        if r.status_code == 404:
            log.debug("get_news(%s): news endpoint not available (sandbox)", ticker)
            return []

        r.raise_for_status()
        data = r.json()

        news_block = data.get("news") or {}
        articles   = news_block.get("article", [])

        if not articles:
            return []
        if isinstance(articles, dict):
            articles = [articles]

        normalised = []
        for a in articles:
            # Parse the datetime string into a Unix timestamp
            pub_ts = 0
            raw_dt = a.get("datetime", "")
            if raw_dt:
                try:
                    dt     = datetime.datetime.fromisoformat(
                        raw_dt.replace("Z", "+00:00")
                    )
                    pub_ts = int(dt.timestamp())
                except Exception:
                    pass

            normalised.append({
                "title":               a.get("title", ""),
                "providerPublishTime": pub_ts,
                "link":                a.get("url", ""),
                "publisher":           a.get("source", ""),
                "summary":             a.get("summary", ""),
            })

        return normalised[:max_articles]

    except Exception as e:
        log.warning("get_news(%s): %s", ticker, e)
        return []


# ─── Options chain → DataFrame ────────────────────────────────────────────────

def chain_to_df(chain: list[dict], option_type: str = "call") -> pd.DataFrame:
    """
    Convert a Tradier options chain list (from tradier_client.get_options_chain)
    into a pandas DataFrame with the same column names options_flow.py uses
    from yfinance's option_chain().calls / .puts.

    Tradier key    →  DataFrame column
    -----------       ----------------
    strike         →  strike
    bid            →  bid
    ask            →  ask
    last           →  lastPrice
    volume         →  volume
    open_interest  →  openInterest
    option_type    →  (used to filter)

    Args:
        chain:       Raw list from get_options_chain()
        option_type: "call" or "put"

    Returns:
        DataFrame ready for options_flow._estimate_call_premium()
    """
    otype = option_type.lower()
    rows  = [
        c for c in chain
        if isinstance(c, dict) and c.get("option_type", "").lower() == otype
    ]

    if not rows:
        return pd.DataFrame(columns=["strike", "bid", "ask",
                                      "lastPrice", "volume", "openInterest"])

    df = pd.DataFrame(rows)

    df = df.rename(columns={
        "last":          "lastPrice",
        "open_interest": "openInterest",
    })

    needed = ["strike", "bid", "ask", "lastPrice", "volume", "openInterest"]
    for col in needed:
        if col not in df.columns:
            df[col] = 0.0

    df = df[needed].apply(pd.to_numeric, errors="coerce").fillna(0)
    return df
