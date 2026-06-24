"""
sandbox_validator.py

Pre-go-live validation script. Run this before switching
TRADIER_SANDBOX=false to confirm every system component is healthy.

Usage:
    python sandbox_validator.py            # full check against sandbox
    python sandbox_validator.py --live     # same checks against live API
    python sandbox_validator.py --quick    # skip slow yfinance data pulls

Each check prints PASS / WARN / FAIL. The script exits with code 1 if
any FAIL is found, so it can be used in a shell gate:
    python sandbox_validator.py && python executor.py
"""

import os
import sys
import argparse
import datetime

# --- ANSI colours (disabled on non-TTY) ----------------------------------------

_USE_COLOUR = sys.stdout.isatty()

def _green(s):  return f"\033[92m{s}\033[0m" if _USE_COLOUR else s
def _yellow(s): return f"\033[93m{s}\033[0m" if _USE_COLOUR else s
def _red(s):    return f"\033[91m{s}\033[0m" if _USE_COLOUR else s
def _bold(s):   return f"\033[1m{s}\033[0m"  if _USE_COLOUR else s


# --- Result tracking ------------------------------------------------------------

_results: list[dict] = []


def _record(name: str, status: str, detail: str = ""):
    """status is 'PASS', 'WARN', or 'FAIL'."""
    _results.append({"name": name, "status": status, "detail": detail})
    colour = {"PASS": _green, "WARN": _yellow, "FAIL": _red}[status]
    label  = colour(f"[{status}]")
    detail_str = f" — {detail}" if detail else ""
    print(f"  {label}  {name}{detail_str}")


def _section(title: str):
    print(f"\n{_bold(title)}")
    print("  " + "-" * 56)


# --- Individual checks ----------------------------------------------------------

def check_env_file():
    _section("Environment / .env")
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        _record(".env file present", "PASS", env_path)
        # Auto-load it so downstream checks see the vars
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
            _record("dotenv loaded", "PASS")
        except ImportError:
            _record("python-dotenv not installed", "WARN",
                    "pip install python-dotenv  (needed for auto .env loading)")
    else:
        _record(".env file", "WARN", f"not found at {env_path} — set env vars manually")


def check_api_keys():
    _section("API Keys")
    api_key    = os.getenv("TRADIER_API_KEY", "")
    account_id = os.getenv("TRADIER_ACCOUNT_ID", "")
    sandbox    = os.getenv("TRADIER_SANDBOX", "true").lower()
    uw_key     = os.getenv("UNUSUAL_WHALES_API_KEY", "")

    if api_key:
        _record("TRADIER_API_KEY", "PASS", f"set ({len(api_key)} chars)")
    else:
        _record("TRADIER_API_KEY", "FAIL", "not set — bot cannot connect to Tradier")

    if account_id:
        _record("TRADIER_ACCOUNT_ID", "PASS", f"set ({account_id})")
    else:
        _record("TRADIER_ACCOUNT_ID", "FAIL", "not set — bot cannot fetch balances or place orders")

    mode = "sandbox" if sandbox == "true" else "LIVE"
    status = "PASS" if sandbox == "true" else "WARN"
    _record(f"TRADIER_SANDBOX={sandbox}", status,
            "safe for testing" if sandbox == "true" else
            "LIVE mode — real money at risk")

    if uw_key:
        _record("UNUSUAL_WHALES_API_KEY", "PASS", f"set ({len(uw_key)} chars)")
    else:
        _record("UNUSUAL_WHALES_API_KEY", "WARN",
                "not set — options flow filter will always return False")


def check_tradier_connection():
    _section("Tradier API Connectivity")
    try:
        from tradier_client import get_account_balance, MOCK_MODE
    except ImportError as e:
        _record("tradier_client import", "FAIL", str(e))
        return

    if MOCK_MODE:
        _record("Tradier connection", "WARN",
                "MOCK_MODE active — no credentials; skipping live checks")
        return

    _record("tradier_client imported", "PASS")

    # Balance fetch
    try:
        balances = get_account_balance()
        equity = balances.get("total_equity", 0)
        if equity > 0:
            _record("Account balance fetch", "PASS",
                    f"equity=${equity:,.2f}")
        else:
            _record("Account balance fetch", "WARN",
                    "equity=$0 — verify account has funds")
    except Exception as e:
        _record("Account balance fetch", "FAIL", str(e))

    # Options chain test on a known liquid ticker
    try:
        from tradier_client import get_options_expirations, get_options_chain
        exps = get_options_expirations("SPY")
        if exps:
            _record("Options expirations (SPY)", "PASS",
                    f"{len(exps)} dates returned")
            # Pull the nearest expiry's chain
            chain = get_options_chain("SPY", exps[0])
            if chain:
                calls = [c for c in chain if c.get("option_type") == "call"]
                _record("Options chain fetch (SPY)", "PASS",
                        f"{len(calls)} calls on {exps[0]}")
            else:
                _record("Options chain fetch (SPY)", "WARN",
                        "chain returned empty — check permissions")
        else:
            _record("Options expirations (SPY)", "FAIL",
                    "no dates returned — check API key / scope")
    except Exception as e:
        _record("Options chain test", "FAIL", str(e))

    # Quote test
    try:
        from tradier_client import get_quote
        price = get_quote("SPY")
        if price > 0:
            _record("Quote fetch (SPY)", "PASS", f"${price:.2f}")
        else:
            _record("Quote fetch (SPY)", "WARN", "returned $0 — market may be closed")
    except Exception as e:
        _record("Quote fetch", "FAIL", str(e))


def check_yfinance(quick: bool):
    _section("yfinance Data Sources")
    try:
        import yfinance as yf
        _record("yfinance imported", "PASS")
    except ImportError:
        _record("yfinance import", "FAIL", "pip install yfinance")
        return

    if quick:
        _record("yfinance data pull", "WARN", "skipped (--quick)")
        return

    # Price data
    try:
        ticker = yf.Ticker("AAPL")
        hist = ticker.history(period="5d")
        if not hist.empty:
            _record("yfinance price data (AAPL 5d)", "PASS",
                    f"{len(hist)} rows, last close ${float(hist['Close'].iloc[-1]):.2f}")
        else:
            _record("yfinance price data", "WARN", "returned empty DataFrame")
    except Exception as e:
        _record("yfinance price data", "FAIL", str(e))

    # News
    try:
        from news_catalyst import fetch_recent_news, _get_article_title, _get_publish_time
        articles = fetch_recent_news("AAPL", max_articles=5)
        if articles:
            titled  = sum(1 for a in articles if _get_article_title(a))
            stamped = sum(1 for a in articles if _get_publish_time(a) is not None)
            _record("yfinance news (AAPL)", "PASS" if titled > 0 else "WARN",
                    f"{len(articles)} articles | {titled} with title | {stamped} with timestamp")
        else:
            _record("yfinance news", "WARN",
                    "returned 0 articles — news filter will be inactive "
                    "(bot degrades gracefully)")
    except Exception as e:
        _record("yfinance news", "FAIL", str(e))

    # Earnings calendar
    try:
        from earnings_filter import get_earnings_date
        date = get_earnings_date("AAPL")
        if date:
            days = (date - datetime.date.today()).days
            _record("Earnings calendar (AAPL)", "PASS", f"{date} ({days}d out)")
        else:
            _record("Earnings calendar (AAPL)", "WARN",
                    "no date found — hardcoded calendar may be stale")
    except Exception as e:
        _record("Earnings calendar", "FAIL", str(e))


def check_unusual_whales(quick: bool):
    _section("Unusual Whales Options Flow")
    uw_key = os.getenv("UNUSUAL_WHALES_API_KEY", "")
    if not uw_key:
        _record("Unusual Whales API key", "WARN",
                "not set — flow filter always returns False; skipping connectivity test")
        return

    if quick:
        _record("Unusual Whales live test", "WARN", "skipped (--quick)")
        return

    try:
        from options_flow import get_bullish_sweep_tickers
        results = get_bullish_sweep_tickers(min_premium=50_000, limit=10)
        if isinstance(results, list):
            _record("Unusual Whales sweep data", "PASS",
                    f"{len(results)} tickers with flow above $50K")
        else:
            _record("Unusual Whales sweep data", "WARN",
                    f"unexpected return type: {type(results)}")
    except Exception as e:
        _record("Unusual Whales connectivity", "FAIL", str(e))


def check_risk_parameters():
    _section("Risk Parameters")
    try:
        from risk_manager import (
            RISK_PER_TRADE_PCT, DAILY_LOSS_LIMIT_PCT,
            MAX_OPEN_POSITIONS, MAX_POSITION_SIZE_PCT,
        )

        if 0 < RISK_PER_TRADE_PCT <= 0.05:
            _record(f"RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT:.1%}", "PASS")
        elif RISK_PER_TRADE_PCT > 0.05:
            _record(f"RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT:.1%}", "WARN",
                    "above 5% per trade — high risk")
        else:
            _record(f"RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT}", "FAIL", "must be > 0")

        if 0 < DAILY_LOSS_LIMIT_PCT <= 0.10:
            _record(f"DAILY_LOSS_LIMIT_PCT={DAILY_LOSS_LIMIT_PCT:.1%}", "PASS")
        else:
            _record(f"DAILY_LOSS_LIMIT_PCT={DAILY_LOSS_LIMIT_PCT:.1%}", "WARN",
                    "unusually high daily loss limit")

        _record(f"MAX_OPEN_POSITIONS={MAX_OPEN_POSITIONS}", "PASS")
        _record(f"MAX_POSITION_SIZE_PCT={MAX_POSITION_SIZE_PCT:.1%}", "PASS")

    except Exception as e:
        _record("risk_manager import", "FAIL", str(e))


def check_required_files():
    _section("Required Files")
    base = os.path.dirname(__file__)
    required = [
        # Scanner pipeline
        "executor.py", "options_scanner.py", "earnings_filter.py",
        "earnings_provider.py", "options_flow.py", "news_catalyst.py",
        "momentum_scorer.py", "option_selector.py",
        # Risk / position / order management
        "risk_manager.py", "position_manager.py", "order_manager.py",
        "tradier_client.py", "state_io.py",
        # Journaling / state
        "trade_journal.py", "decision_log.py", "equity_tracker.py",
        "analytics_metrics.py", "event_notifier.py", "heartbeat.py",
        # Infrastructure
        "logger.py", "utils.py", "universe.py", "market_filter.py",
        # Dashboard
        "dashboard_state.py", "dashboard_server.py", "dashboard.html",
    ]
    optional = [
        # Strategies
        "iv_rank_bot.py", "hft_scanner.py", "hft_executor.py", "backtest_hft.py",
        # Ops / tools
        "start_all.py", "kill_switch.py", "daily_report.py",
        "walk_forward.py", "lifecycle_validator.py",
    ]
    for fname in required:
        path = os.path.join(base, fname)
        if os.path.exists(path):
            _record(fname, "PASS")
        else:
            _record(fname, "FAIL", "missing — bot cannot start")

    for fname in optional:
        path = os.path.join(base, fname)
        if os.path.exists(path):
            _record(fname, "PASS", "optional strategy available")
        else:
            _record(fname, "WARN", "optional — not required for core bot")


def check_python_deps():
    _section("Python Dependencies")
    deps = {
        "requests":    "requests",
        "pandas":      "pandas",
        "yfinance":    "yfinance",
        "ta":          "ta",
        "dotenv":      "python-dotenv",
    }
    for module, package in deps.items():
        try:
            __import__(module)
            _record(f"{module}", "PASS")
        except ImportError:
            _record(f"{module}", "FAIL", f"pip install {package}")


# --- Summary --------------------------------------------------------------------

def print_summary():
    total  = len(_results)
    passed = sum(1 for r in _results if r["status"] == "PASS")
    warned = sum(1 for r in _results if r["status"] == "WARN")
    failed = sum(1 for r in _results if r["status"] == "FAIL")

    print("\n" + "=" * 60)
    print(_bold("  VALIDATION SUMMARY"))
    print("=" * 60)
    print(f"  {_green(f'PASS: {passed}'):<20}  {_yellow(f'WARN: {warned}'):<20}  {_red(f'FAIL: {failed}')}")

    if failed:
        print(f"\n  {_red('NOT READY FOR LIVE TRADING')}")
        print("  Fix all FAIL items before switching TRADIER_SANDBOX=false.\n")
        fails = [r for r in _results if r["status"] == "FAIL"]
        for r in fails:
            print(f"    {_red('X')} {r['name']}: {r['detail']}")
    elif warned:
        print(f"\n  {_yellow('READY WITH CAVEATS')} — review WARN items above")
        print("  Bot will run but some features may be degraded.\n")
    else:
        print(f"\n  {_green('ALL CLEAR')} — bot is ready for live trading.\n")

    print("=" * 60 + "\n")
    return failed


# --- Entry point ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MawiTek pre-go-live validation."
    )
    parser.add_argument("--live",  action="store_true",
                        help="Validate against live Tradier API (default: sandbox)")
    parser.add_argument("--quick", action="store_true",
                        help="Skip slow yfinance data pulls")
    args = parser.parse_args()

    if args.live:
        os.environ["TRADIER_SANDBOX"] = "false"

    print("\n" + "=" * 60)
    print(_bold("  MAWITEK SANDBOX VALIDATOR"))
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "LIVE" if args.live else "SANDBOX"
    print(f"  Mode: {_yellow(mode) if args.live else _green(mode)}")
    print("=" * 60)

    check_env_file()
    check_python_deps()
    check_api_keys()
    check_required_files()
    check_risk_parameters()
    check_tradier_connection()
    check_yfinance(quick=args.quick)
    check_unusual_whales(quick=args.quick)

    failures = print_summary()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
