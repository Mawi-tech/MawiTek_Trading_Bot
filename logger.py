"""
logger.py

Centralised logging for the MawiTek trading bot.

Usage:
    from logger import get_logger, log_trade

    log = get_logger("executor")
    log.info("Placing order for AAPL")
    log.warning("Budget too small for 1 contract")
    log.error("Order failed: %s", err)

    log_trade({...})   # appends one JSON line to logs/trades.jsonl

Log files (all in logs/):
    executor.log        Swing catalyst strategy
    hft_executor.log    Intraday HFT strategy
    iv_rank_bot.log     IV-rank premium strategy
    dashboard.log       Dashboard server
    risk_manager.log    Risk decisions shared by all strategies
    trades.jsonl        Structured trade log — one JSON line per decision
                        (both accepted AND rejected — filter on "approved")
"""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# ── Fix Windows console encoding ──────────────────────────────────────────────
# Windows cmd/PowerShell defaults to cp1252 which can't encode emoji or many
# Unicode chars used in log messages. Force UTF-8 on stdout/stderr so any
# print() or console log handler never raises UnicodeEncodeError.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        # Python < 3.7 fallback — shouldn't happen with 3.13 but just in case
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TRADE_LOG = os.path.join(LOG_DIR, "trades.jsonl")

# Shared format — wide enough to align component names nicely
_FMT = logging.Formatter(
    "%(asctime)s [%(name)-16s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    """
    Return a named logger that writes to both console (INFO+) and a
    rotating file (DEBUG+, 10 MB × 5 back-ups).

    Calling get_logger() with the same name twice returns the same
    instance — handlers are never duplicated.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    os.makedirs(LOG_DIR, exist_ok=True)

    # ── Rotating file handler (full detail) ───────────────────────────
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FMT)

    # ── Console handler (INFO and above only) ─────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(_FMT)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def log_trade(record: dict) -> None:
    """
    Append one structured record to logs/trades.jsonl.

    Every trade decision — approved AND rejected — should be logged here
    so you can audit why the bot did (or didn't) trade any given setup.

    Recommended fields:
        timestamp       ISO-8601 string (added automatically if absent)
        strategy        "swing" | "hft" | "iv_rank"
        ticker          Underlying symbol
        approved        bool — was the trade placed?
        reason          Human-readable decision summary
        setup_score     Composite scanner score (swing/hft only)
        option_symbol   OCC symbol (if contract was selected)
        strike          Strike price
        expiration      Expiration date string
        dte             Days to expiry at entry
        entry_price     Mid-price at order time
        quantity        Contracts ordered
        order_type      "limit" | "market"
        limit_price     Limit price sent to broker (if applicable)
        cost_estimate   quantity × entry_price × 100
        equity          Account equity at time of check
        budget          Max $ allocated to this trade
        daily_pnl       Running P&L at time of decision
        filters_passed  Dict of which scanner filters fired (swing only)
        direction       "bullish" | "bearish" (hft only)
        signals         List of active intraday signals (hft only)
        exit_reason     Populated on close: "take_profit" | "stop_loss" | ...
        exit_price      Mid-price at exit
        pnl_pct         Percentage gain/loss on exit
        pnl_dollar      Dollar gain/loss on exit
    """
    import datetime as _dt

    record.setdefault("timestamp", _dt.datetime.now().isoformat())
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
