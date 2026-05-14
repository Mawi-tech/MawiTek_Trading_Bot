"""
hft_scanner.py  —  Strategy 3: Intraday Momentum Scanner

Scans for high-probability short-term directional moves using
intraday price and volume data. Targets 0–1 DTE options for
quick scalps with 15–60 minute hold times.

Signals detected:
    1. VWAP Reclaim    — price crosses above VWAP after a dip, with vol surge
    2. Opening Range Breakout (ORB) — price breaks the first 15-min high/low
    3. Volume Spike    — sudden vol >= 3x rolling average on up-bar
    4. Momentum Burst  — short-term ROC + RSI confirmation

Each signal is scored 0–100. Only setups >= MIN_SIGNAL_SCORE are returned.

Usage:
    from hft_scanner import run_hft_scan
    setups = run_hft_scan()

    # CLI
    python hft_scanner.py
    python hft_scanner.py --interval 1m --limit 50
"""

import datetime
import math
import argparse

import numpy as np
import pandas as pd
import yfinance as yf

from universe import load_universe
from market_filter import filter_universe


# ─── Config ────────────────────────────────────────────────────────────────────

DEFAULT_INTERVAL    = "5m"      # Bar size: "1m", "2m", "5m"
DEFAULT_PERIOD      = "1d"      # How far back to pull (intraday max 7d)
ORB_MINUTES         = 15        # Opening range window in minutes
VWAP_VOL_MULT       = 2.0       # Min vol multiplier for VWAP reclaim signal
SPIKE_VOL_MULT      = 3.0       # Min vol multiplier for raw spike signal
MIN_SIGNAL_SCORE    = 60        # Minimum score to include in output
UNIVERSE_LIMIT      = 75        # Symbols to scan per cycle
MIN_PRICE           = 5.0       # Skip penny stocks
MIN_AVG_VOLUME      = 1_000_000 # Liquidity floor (daily avg shares)
MIN_DOLLAR_VOLUME   = 10_000_000

# ── Session gate ───────────────────────────────────────────────────────────────
# Only fire signals during "prime time" — avoids opening noise and EOD entries
# that carry overnight and gap against us.  Times are UTC.
# 10:00 AM EST = 14:00 UTC  |  2:30 PM EST = 18:30 UTC
PRIME_SESSION_START_UTC = datetime.time(14, 0)   # 10:00 AM EST
PRIME_SESSION_END_UTC   = datetime.time(18, 30)  # 2:30 PM EST

# Inverse / leveraged-inverse ETFs: bullish momentum signals are structurally
# wrong on these instruments.  The executor will skip any ticker on this list.
INVERSE_ETF_LIST = {
    "SQQQ", "SDOW", "SPXS", "SPXU", "TECS", "FAZ", "LABD",
    "SOXS", "UVXY", "SVXY", "VIXY", "SDS", "PSQ", "DOG",
}

# EMA window used for trend alignment filter
TREND_EMA_WINDOW = 20


# ─── Data Fetching ─────────────────────────────────────────────────────────────

def fetch_intraday(ticker: str, interval: str = DEFAULT_INTERVAL,
                   period: str = DEFAULT_PERIOD) -> pd.DataFrame:
    """
    Pull intraday OHLCV bars from yfinance.
    Returns a clean DataFrame with columns: Open, High, Low, Close, Volume.
    Returns empty DataFrame on any failure.
    """
    try:
        df = yf.download(
            tickers=ticker,
            interval=interval,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        print(f"[HFT Scanner] Error fetching {ticker} ({interval}): {e}")
        return pd.DataFrame()


# ─── Indicators ────────────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP (reset per trading day)."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_tpv = (typical * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=window - 1, min_periods=window).mean()
    avg_l = loss.ewm(com=window - 1, min_periods=window).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_rolling_vol_avg(volume: pd.Series, window: int = 20) -> pd.Series:
    return volume.rolling(window, min_periods=5).mean()


def compute_ema(close: pd.Series, window: int = TREND_EMA_WINDOW) -> pd.Series:
    return close.ewm(span=window, adjust=False).mean()


def is_prime_session(bar_time: pd.Timestamp) -> bool:
    """
    Returns True when the bar falls inside the prime intraday window.

    yfinance returns timestamps in UTC for US equities.
    Prime window: 10:00 AM – 2:30 PM EST  =  14:00 – 18:30 UTC.

    Bars outside this window are:
      - Pre/post-market noise        (before 14:00 UTC)
      - EOD setups that carry overnight  (after 18:30 UTC)
    """
    try:
        t = bar_time.time()
        # Strip timezone info for comparison if present
        t = datetime.time(t.hour, t.minute, t.second)
        return PRIME_SESSION_START_UTC <= t <= PRIME_SESSION_END_UTC
    except Exception:
        return True   # Fail open — don't block on timezone edge cases


def detect_trend_alignment(df: pd.DataFrame) -> dict:
    """
    Checks whether price is trending in a clear direction using EMA(20).

    Bullish alignment:  close > EMA AND close > VWAP-ish (last 5 bars rising)
    Bearish alignment:  close < EMA AND close falling

    Returns:
        {
            "aligned_bullish": bool,
            "aligned_bearish": bool,
            "score_bonus":     int   (+15 if aligned with direction, -10 if counter)
            "detail":          str
        }
    """
    if len(df) < TREND_EMA_WINDOW + 2:
        return {"aligned_bullish": False, "aligned_bearish": False,
                "score_bonus": 0, "detail": "insufficient bars for EMA"}

    close = df["Close"].astype(float)
    ema   = compute_ema(close)

    last_close = float(close.iloc[-1])
    last_ema   = float(ema.iloc[-1])
    prev_ema   = float(ema.iloc[-3])

    ema_rising  = last_ema > prev_ema
    ema_falling = last_ema < prev_ema

    above_ema = last_close > last_ema
    below_ema = last_close < last_ema

    aligned_bullish = above_ema and ema_rising
    aligned_bearish = below_ema and ema_falling

    pct_vs_ema = round((last_close - last_ema) / last_ema * 100, 2) if last_ema > 0 else 0

    detail = (
        f"EMA({TREND_EMA_WINDOW})={last_ema:.2f} | "
        f"price {'above' if above_ema else 'below'} EMA by {abs(pct_vs_ema):.2f}% | "
        f"EMA {'rising' if ema_rising else 'falling'}"
    )

    return {
        "aligned_bullish": aligned_bullish,
        "aligned_bearish": aligned_bearish,
        "score_bonus":     0,   # Applied in score_hft_setup based on direction
        "detail":          detail,
        "pct_vs_ema":      pct_vs_ema,
    }


def get_opening_range(df: pd.DataFrame, orb_minutes: int = ORB_MINUTES
                      ) -> tuple[float, float] | None:
    """
    Returns (orb_high, orb_low) from the first `orb_minutes` of today's session.
    Returns None if there is not enough data.
    """
    today = df.index[-1].date()
    today_bars = df[df.index.date == today]
    if today_bars.empty:
        return None

    session_start = today_bars.index[0]
    orb_end = session_start + datetime.timedelta(minutes=orb_minutes)
    orb_bars = today_bars[today_bars.index <= orb_end]

    if orb_bars.empty:
        return None

    return float(orb_bars["High"].max()), float(orb_bars["Low"].min())


# ─── Signal Detection ──────────────────────────────────────────────────────────

def detect_vwap_reclaim(df: pd.DataFrame, vwap: pd.Series) -> dict:
    """
    VWAP Reclaim: last bar closed above VWAP after at least one bar below,
    with volume >= VWAP_VOL_MULT × rolling average.

    Returns: {"signal": bool, "score": int, "detail": str}
    """
    if len(df) < 5:
        return {"signal": False, "score": 0, "detail": "insufficient bars"}

    close   = df["Close"]
    vol     = df["Volume"]
    vol_avg = compute_rolling_vol_avg(vol)

    last_close = float(close.iloc[-1])
    last_vwap  = float(vwap.iloc[-1])
    prev_close = float(close.iloc[-2])
    prev_vwap  = float(vwap.iloc[-2])
    last_vol   = float(vol.iloc[-1])
    avg_vol    = float(vol_avg.iloc[-1]) if not math.isnan(float(vol_avg.iloc[-1])) else 1

    # Crossover: prev below VWAP, current above
    crossed = (prev_close < prev_vwap) and (last_close > last_vwap)
    vol_confirmed = (avg_vol > 0) and (last_vol / avg_vol >= VWAP_VOL_MULT)

    if not crossed:
        return {"signal": False, "score": 0, "detail": "no VWAP crossover"}

    score = 30  # Base for crossover
    if vol_confirmed:
        score += 30
    vol_ratio = round(last_vol / avg_vol, 2) if avg_vol > 0 else 0
    pct_above = round((last_close - last_vwap) / last_vwap * 100, 2) if last_vwap > 0 else 0

    if pct_above >= 0.5:
        score += 20
    elif pct_above >= 0.2:
        score += 10

    return {
        "signal": True,
        "score":  min(score, 80),
        "detail": f"VWAP reclaim | vol {vol_ratio:.1f}x avg | +{pct_above:.2f}% above VWAP",
    }


def detect_orb_breakout(df: pd.DataFrame) -> dict:
    """
    Opening Range Breakout: price breaks above ORB high on volume.

    Returns: {"signal": bool, "score": int, "detail": str, "direction": str}
    """
    orb = get_opening_range(df, ORB_MINUTES)
    if orb is None:
        return {"signal": False, "score": 0, "detail": "no ORB data", "direction": "none"}

    orb_high, orb_low = orb
    last_close = float(df["Close"].iloc[-1])
    last_vol   = float(df["Volume"].iloc[-1])
    vol_avg    = float(compute_rolling_vol_avg(df["Volume"]).iloc[-1])

    # Must be past the ORB window
    today       = df.index[-1].date()
    today_bars  = df[df.index.date == today]
    session_start = today_bars.index[0]
    orb_end       = session_start + datetime.timedelta(minutes=ORB_MINUTES)
    if df.index[-1] <= orb_end:
        return {"signal": False, "score": 0, "detail": "still in ORB window", "direction": "none"}

    vol_ok = (vol_avg > 0) and (last_vol / vol_avg >= 1.5)

    if last_close > orb_high:
        score = 40
        if vol_ok:
            score += 25
        pct_break = round((last_close - orb_high) / orb_high * 100, 2)
        if pct_break >= 0.5:
            score += 15
        return {
            "signal":    True,
            "score":     min(score, 80),
            "direction": "bullish",
            "detail":    f"ORB breakout above ${orb_high:.2f} by +{pct_break:.2f}%",
        }

    if last_close < orb_low:
        score = 40
        if vol_ok:
            score += 20
        pct_break = round((orb_low - last_close) / orb_low * 100, 2)
        return {
            "signal":    True,
            "score":     min(score, 60),
            "direction": "bearish",
            "detail":    f"ORB breakdown below ${orb_low:.2f} by -{pct_break:.2f}%",
        }

    return {"signal": False, "score": 0, "detail": "inside ORB range", "direction": "none"}


def detect_volume_spike(df: pd.DataFrame) -> dict:
    """
    Standalone volume spike on an up-bar (close > open).

    Returns: {"signal": bool, "score": int, "detail": str}
    """
    if len(df) < 10:
        return {"signal": False, "score": 0, "detail": "insufficient bars"}

    last  = df.iloc[-1]
    vol   = df["Volume"]
    vol_avg = float(compute_rolling_vol_avg(vol).iloc[-1])

    if math.isnan(vol_avg) or vol_avg <= 0:
        return {"signal": False, "score": 0, "detail": "vol avg unavailable"}

    vol_ratio  = float(last["Volume"]) / vol_avg
    is_up_bar  = float(last["Close"]) > float(last["Open"])

    if vol_ratio < SPIKE_VOL_MULT or not is_up_bar:
        return {"signal": False, "score": 0,
                "detail": f"vol {vol_ratio:.1f}x (need {SPIKE_VOL_MULT}x) / up={is_up_bar}"}

    score = 25
    if vol_ratio >= 5.0:
        score += 25
    elif vol_ratio >= 4.0:
        score += 20
    elif vol_ratio >= 3.0:
        score += 10

    return {
        "signal": True,
        "score":  min(score, 60),
        "detail": f"Volume spike {vol_ratio:.1f}x avg on up-bar",
    }


def detect_momentum_burst(df: pd.DataFrame) -> dict:
    """
    Short-term momentum: 5-bar ROC + RSI rising and in healthy zone.

    Returns: {"signal": bool, "score": int, "detail": str}
    """
    if len(df) < 20:
        return {"signal": False, "score": 0, "detail": "insufficient bars"}

    close = df["Close"].astype(float)
    rsi   = compute_rsi(close, window=14)

    rsi_now  = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-5])

    roc_5 = (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100

    rsi_rising  = rsi_now > rsi_prev
    rsi_healthy = 50 <= rsi_now <= 75

    if not rsi_rising or roc_5 <= 0:
        return {"signal": False, "score": 0,
                "detail": f"ROC={roc_5:.2f}% RSI={rsi_now:.1f} rising={rsi_rising}"}

    score = 0
    if roc_5 >= 2.0:
        score += 30
    elif roc_5 >= 1.0:
        score += 20
    elif roc_5 >= 0.5:
        score += 10

    if rsi_rising and rsi_healthy:
        score += 20
    elif rsi_rising:
        score += 10

    return {
        "signal": score >= 20,
        "score":  min(score, 50),
        "detail": f"ROC5={roc_5:.2f}% | RSI={rsi_now:.1f} ({'↑' if rsi_rising else '↓'})",
    }


# ─── Composite Scoring ─────────────────────────────────────────────────────────

def score_hft_setup(signals: dict, direction: str = "bullish") -> int:
    """
    Combine individual signal scores into a composite setup score (0–100).
    Weights: ORB breakout > VWAP reclaim > vol spike > momentum burst.

    Applies a trend-alignment bonus/penalty based on EMA direction:
      +15 pts  if trade aligns with the EMA trend
      -15 pts  if trade is counter-trend (strong fade filter)
    """
    orb_score      = signals.get("orb", {}).get("score", 0)
    vwap_score     = signals.get("vwap", {}).get("score", 0)
    spike_score    = signals.get("spike", {}).get("score", 0)
    momentum_score = signals.get("momentum", {}).get("score", 0)
    trend          = signals.get("trend", {})

    # Confluence count — only the three CORE signals (VWAP + ORB + Spike).
    # Momentum is intentionally excluded: the 503-ticker × 60-day backtest
    # showed it acts as a noise amplifier rather than a confirmation signal.
    # Counting it here would double-pay setups (once via weight, once via
    # confluence multiplier) and inflate weak FOMO chases.
    active = sum(1 for s in [orb_score, vwap_score, spike_score] if s > 0)

    # Weighted composite — momentum demoted to a tiebreaker.
    #
    # 503-ticker / 60-day backtest results, per-combo per-trade P&L:
    #   [vwap, orb, spike]            n=21   avg +$70.96   total +$1,490
    #   [vwap, orb, spike, momentum]  n=53   avg +$7.44    total +$394
    #
    # Same triple gate, ~10x worse outcome when momentum also fires.
    # Conclusion: momentum lets in lower-quality FOMO-chase setups.
    # Old weights:  orb 0.30 | vwap 0.20 | spike 0.30 | momentum 0.20
    # New weights:  orb 0.35 | vwap 0.30 | spike 0.30 | momentum 0.05
    raw = (
        orb_score      * 0.35 +
        vwap_score     * 0.30 +
        spike_score    * 0.30 +
        momentum_score * 0.05
    )

    # Hard gates: VWAP reclaim + ORB breakout + volume spike must ALL fire.
    # 60-day backtesting confirmed per-combo P&L:
    #   vwap+orb+spike          →  +$262/trade (4 trades)
    #   vwap+orb+spike+momentum →  +$74/trade  (8 trades)
    #   vwap+spike (no ORB)     →  -$44/trade  (7 trades) ← eliminated
    #   orb+spike  (no VWAP)    →  -$3.54/trade (166 trades) ← eliminated
    # All three signals together = confirmed momentum with institutional flow.
    if spike_score == 0 or vwap_score == 0 or orb_score == 0:
        return 0

    # Confluence bonus
    if active >= 3:
        raw *= 1.25
    elif active >= 2:
        raw *= 1.10

    # Trend alignment modifier
    if trend:
        if direction == "bullish" and trend.get("aligned_bullish"):
            raw += 15   # Trading with the trend
        elif direction == "bullish" and trend.get("aligned_bearish"):
            raw -= 15   # Counter-trend — penalise heavily
        elif direction == "bearish" and trend.get("aligned_bearish"):
            raw += 15
        elif direction == "bearish" and trend.get("aligned_bullish"):
            raw -= 15

    return max(0, min(100, int(raw)))


# ─── Main Scanner ──────────────────────────────────────────────────────────────

def scan_ticker(
    ticker: str,
    interval: str = DEFAULT_INTERVAL,
) -> dict | None:
    """
    Run all intraday signals for a single ticker.
    Returns a setup dict if the composite score >= MIN_SIGNAL_SCORE, else None.

    Gates applied before scoring:
      1. Inverse/leveraged-inverse ETF exclusion
      2. Prime session window (10 AM – 2:30 PM EST)
      3. Trend alignment (EMA20) as score modifier
    """
    # Gate 1: inverse ETF exclusion
    if ticker.upper() in INVERSE_ETF_LIST:
        return None

    df = fetch_intraday(ticker, interval=interval)
    if df.empty or len(df) < 15:
        return None

    # Gate 2: session time — last bar must be inside prime window
    last_bar_time = df.index[-1]
    if not is_prime_session(last_bar_time):
        return None

    vwap = compute_vwap(df)

    # Determine dominant direction before scoring (needed for trend modifier)
    orb_result = detect_orb_breakout(df)
    vwap_result = detect_vwap_reclaim(df, vwap)
    orb_dir    = orb_result.get("direction", "none")
    vwap_sig   = vwap_result.get("signal", False)
    direction  = orb_dir if orb_dir in ("bullish", "bearish") else (
        "bullish" if vwap_sig else "bullish"
    )

    signals = {
        "vwap":     vwap_result,
        "orb":      orb_result,
        "spike":    detect_volume_spike(df),
        "momentum": detect_momentum_burst(df),
        "trend":    detect_trend_alignment(df),   # Gate 3: EMA filter
    }

    score = score_hft_setup(signals, direction=direction)
    if score < MIN_SIGNAL_SCORE:
        return None

    last      = df.iloc[-1]
    last_vwap = float(vwap.iloc[-1])

    active_signals = [
        name for name, s in signals.items()
        if name != "trend" and s.get("signal", False)
    ]

    return {
        "ticker":         ticker,
        "setup_score":    score,
        "direction":      direction,
        "last_price":     round(float(last["Close"]), 2),
        "vwap":           round(last_vwap, 2),
        "pct_vs_vwap":    round((float(last["Close"]) - last_vwap) / last_vwap * 100, 2),
        "trend_aligned":  signals["trend"].get(f"aligned_{direction}", False),
        "pct_vs_ema":     signals["trend"].get("pct_vs_ema", 0),
        "interval":       interval,
        "active_signals": active_signals,
        "signal_details": {
            name: s.get("detail", "")
            for name, s in signals.items()
            if name != "trend" and s.get("signal", False)
        },
        "timestamp":      df.index[-1].isoformat(),
    }


def run_hft_scan(
    csv_path: str | None = "sp500.csv",
    universe_limit: int = UNIVERSE_LIMIT,
    interval: str = DEFAULT_INTERVAL,
    min_score: int = MIN_SIGNAL_SCORE,
) -> list[dict]:
    """
    Full intraday scan pipeline.

    Returns a list of setup dicts sorted by score descending.
    """
    print("\n" + "=" * 60)
    print("  HFT INTRADAY SCANNER  —  Strategy 3")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  interval={interval}")
    print("=" * 60 + "\n")

    symbols = load_universe(csv_path=csv_path, limit=universe_limit)
    liquid  = filter_universe(
        symbols=symbols,
        min_price=MIN_PRICE,
        min_avg_volume=MIN_AVG_VOLUME,
        min_avg_dollar_volume=MIN_DOLLAR_VOLUME,
    )
    print(f"[HFT Scanner] {len(liquid)} liquid symbols to scan\n")

    results = []
    for ticker in liquid:
        setup = scan_ticker(ticker, interval=interval)
        if setup and setup["setup_score"] >= min_score:
            results.append(setup)
            sigs = ", ".join(setup["active_signals"]) or "none"
            print(
                f"[HFT Scanner] ✅ {ticker} | Score: {setup['setup_score']}/100 | "
                f"Dir: {setup['direction']} | Signals: {sigs}"
            )

    results.sort(key=lambda x: x["setup_score"], reverse=True)

    print("\n" + "=" * 60)
    print(f"  HFT SETUPS FOUND: {len(results)}")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        print(
            f"\n#{i} {r['ticker']} | Score: {r['setup_score']}/100 | {r['direction'].upper()}"
            f"\n    Price: ${r['last_price']} | VWAP: ${r['vwap']} | "
            f"vs VWAP: {r['pct_vs_vwap']:+.2f}%"
            f"\n    Signals: {', '.join(r['active_signals'])}"
        )
        for sig, detail in r["signal_details"].items():
            print(f"    [{sig}] {detail}")

    return results


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HFT Intraday Scanner — Strategy 3")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL,
                        choices=["1m", "2m", "5m"],
                        help="Bar interval (default 5m)")
    parser.add_argument("--limit",    type=int, default=UNIVERSE_LIMIT,
                        help=f"Universe size (default {UNIVERSE_LIMIT})")
    parser.add_argument("--min-score", type=int, default=MIN_SIGNAL_SCORE,
                        help=f"Minimum score threshold (default {MIN_SIGNAL_SCORE})")
    args = parser.parse_args()

    run_hft_scan(
        universe_limit=args.limit,
        interval=args.interval,
        min_score=args.min_score,
    )
