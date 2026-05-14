import os

# ─── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD"]

# ─── Stock Signal Config ───────────────────────────────────────────────────────
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

STOP_LOSS = 0.10          # 10%
TAKE_PROFIT_MIN = 0.15
TAKE_PROFIT_MAX = 0.20

TIMEFRAME = "1h"

# ─── Options Scanner Config ────────────────────────────────────────────────────
EARNINGS_MIN_DAYS = 1
EARNINGS_MAX_DAYS = 5

OPTIONS_FLOW_MIN_PREMIUM = 50_000   # $50K minimum in call sweeps

NEWS_MIN_SCORE = 1
NEWS_LOOKBACK_HOURS = 48

MOMENTUM_MIN_SCORE = 40             # Out of 100

# ─── API Keys (load from .env) ─────────────────────────────────────────────────
UNUSUAL_WHALES_API_KEY = os.getenv("UNUSUAL_WHALES_API_KEY", "")
