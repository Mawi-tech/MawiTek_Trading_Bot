"""
backtest_hft.py  —  Strategy 3: HFT Intraday Backtester

Backtests the hft_scanner signals against historical intraday data
using a simple option P&L model.

Because historical option chains aren't available via free APIs, P&L is
estimated using a Black-Scholes approximation:

    option_price ≈ stock_move * delta + 0.5 * gamma * stock_move²

where delta and gamma are estimated from at-the-money approximations
using the ticker's historical volatility.

This gives a realistic but conservative P&L estimate — real results may
vary due to IV crush, spread costs, and liquidity.

Usage:
    python backtest_hft.py                           # default: last 30 days, SPY/QQQ/AAPL
    python backtest_hft.py --tickers TSLA NVDA AMD
    python backtest_hft.py --days 60 --interval 5m
    python backtest_hft.py --tickers AAPL --days 14 --show-trades
    python backtest_hft.py --universe                # full S&P 500
    python backtest_hft.py --universe --max-tickers 100 --days 60
"""

import argparse
import datetime
import math
import sys

import numpy as np
import pandas as pd
import yfinance as yf

from hft_scanner import (
    fetch_intraday,
    compute_vwap,
    detect_vwap_reclaim,
    detect_orb_breakout,
    detect_volume_spike,
    detect_momentum_burst,
    detect_trend_alignment,
    score_hft_setup,
    is_prime_session,
    INVERSE_ETF_LIST,
    MIN_SIGNAL_SCORE,
)
from universe import DEFAULT_UNIVERSE


# --- S&P 500 Universe Loader --------------------------------------------------

def fetch_sp500_tickers(max_tickers: int | None = None) -> list[str]:
    """
    Fetch the current S&P 500 component list from Wikipedia.

    Uses a browser User-Agent to avoid 403 blocks; falls back to
    SP500_FALLBACK if the fetch fails.

    Args:
        max_tickers: Optional cap — useful for faster test runs.
    Returns:
        List of ticker strings, e.g. ['AAPL', 'MSFT', ...].
    """
    import io
    import requests as _req

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }

    try:
        resp = _req.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text), attrs={"id": "constituents"})
        df = tables[0]
        # Column is usually "Symbol" or "Ticker symbol"
        sym_col = next(
            (c for c in df.columns if "symbol" in c.lower() or "ticker" in c.lower()),
            df.columns[0],
        )
        tickers = (
            df[sym_col]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .str.replace(".", "-", regex=False)   # BRK.B -> BRK-B
            .tolist()
        )
        # Remove duplicates, preserve order
        seen: set[str] = set()
        unique: list[str] = []
        for t in tickers:
            if t and t not in seen:
                seen.add(t)
                unique.append(t)
        print(f"[Universe] Fetched {len(unique)} S&P 500 tickers from Wikipedia.")
        if max_tickers:
            unique = unique[:max_tickers]
            print(f"[Universe] Capped at {len(unique)} tickers (--max-tickers).")
        return unique
    except Exception as exc:
        print(f"[Universe] Wikipedia fetch failed ({exc}); using SP500_FALLBACK list.")
        tickers = list(SP500_FALLBACK)
        if max_tickers:
            tickers = tickers[:max_tickers]
        return tickers


# Hardcoded S&P 500 fallback (May 2025 constituents, ~503 symbols).
# Used when Wikipedia is unreachable. Update periodically.
SP500_FALLBACK = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB",
    "AKAM","ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN",
    "AMCR","AEE","AAL","AEP","AXP","AIG","AMT","AWK","AMP","AME","AMGN",
    "APH","ADI","ANSS","AON","APA","AAPL","AMAT","APTV","ACGL","ADM","ANET",
    "AJG","AIZ","T","ATO","ADSK","ADP","AZO","AVB","AVY","AXON","BKR","BALL",
    "BAC","BK","BBWI","BAX","BDX","BRK-B","BBY","TECH","BIIB","BLK","BX",
    "BA","BKNG","BWA","BSX","BMY","AVGO","BR","BRO","BF-B","BLDR","BG",
    "CDNS","CZR","CPT","CPB","COF","CAH","KMX","CCL","CARR","CTLT","CAT",
    "CBOE","CBRE","CDW","CE","COR","CNC","CNX","CDAY","CF","CRL","SCHW",
    "CHTR","CVX","CMG","CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX",
    "CME","CMS","KO","CTSH","CL","CMCSA","CMA","CAG","COP","ED","STZ","CEG",
    "COO","CPRT","GLW","CTVA","CSGP","COST","CTRA","CCI","CSX","CMI","CVS",
    "DHI","DHR","DRI","DVA","DAY","DECK","DE","DELL","DAL","DVN","DXCM",
    "FANG","DLR","DFS","DG","DLTR","D","DPZ","DOV","DOW","DHI","DTE","DUK",
    "DD","EMN","ETN","EBAY","ECL","EIX","EW","EA","ELV","LLY","EMR","ENPH",
    "ETR","EOG","EPAM","EQT","EFX","EQIX","EQR","ESS","EL","ETSY","EG",
    "EVRG","ES","EXC","EXPE","EXPD","EXR","XOM","FFIV","FDS","FICO","FAST",
    "FRT","FDX","FIS","FITB","FSLR","FE","FI","FLT","FMC","F","FTNT","FTV",
    "FOXA","FOX","BEN","FCX","GRMN","IT","GE","GEHC","GEV","GEN","GNRC",
    "GD","GIS","GM","GPC","GILD","GPN","GL","GDDY","GS","HAL","HIG","HAS",
    "HCA","DOC","HSIC","HSY","HES","HPE","HLT","HOLX","HD","HON","HRL",
    "HST","HWM","HPQ","HUBB","HUM","HBAN","HII","IBM","IEX","IDXX","ITW",
    "INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU","ISRG","IVZ",
    "INVH","IQV","IRM","JBHT","JBL","JKHY","J","JNJ","JCI","JPM","JNPR",
    "K","KVUE","KDP","KEY","KEYS","KMB","KIM","KMI","KLAC","KHC","KR",
    "LHX","LH","LRCX","LW","LVS","LDOS","LEN","LNC","LIN","LYV","LKQ",
    "LMT","L","LOW","LULU","LYB","MTB","MRO","MPC","MKTX","MAR","MMC","MLM",
    "MAS","MA","MTCH","MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM",
    "MCHP","MU","MSFT","MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR","MNST",
    "MCO","MS","MOS","MSI","MSCI","NDAQ","NTAP","NFLX","NEM","NWSA","NWS",
    "NEE","NKE","NI","NDSN","NSC","NTRS","NOC","NCLH","NRG","NUE","NVDA",
    "NVR","NXPI","ORLY","OXY","ODFL","OMC","ON","OKE","ORCL","OTIS","PCAR",
    "PKG","PLTR","PH","PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG","PM",
    "PSX","PNW","PXD","PNC","POOL","PPG","PPL","PFG","PG","PGR","PLD","PRU",
    "PEG","PTVE","PTC","PSA","PHM","QRVO","PWR","QCOM","DGX","RL","RJF",
    "RTX","O","REG","REGN","RF","RSG","RMD","RVTY","ROK","ROL","ROP","ROST",
    "RCL","SPGI","CRM","SBAC","SLB","STX","SRE","NOW","SHW","SPG","SWKS",
    "SJM","SW","SNA","SOLV","SO","LUV","SWK","SBUX","STT","STLD","STE",
    "SYK","SMCI","SYF","SNPS","SYY","TMUS","TROW","TTWO","TPR","TRGP","TGT",
    "TEL","TDY","TFX","TER","TSLA","TXN","TXT","TMO","TJX","TSCO","TT","TDG",
    "TRV","TRMB","TFC","TYL","TSN","USB","UBER","UDR","ULTA","UNP","UAL",
    "UPS","URI","UNH","UHS","VLO","VTR","VLTO","VRSN","VRSK","VZ","VRTX",
    "VTRS","VICI","V","VMC","WRB","GWW","WAB","WBA","WMT","DIS","WBD","WM",
    "WAT","WEC","WFC","WELL","WST","WDC","WRK","WY","WHR","WMB","WTW","WYNN",
    "XEL","XYL","YUM","ZBRA","ZBH","ZTS",
    # Large-caps often traded that may not be in exact constituents:
    "SPY","QQQ","IWM","DIA","ARM","MSTR","COIN","HOOD","RBLX","TTD","DDOG",
    "ZS","SNOW","NET","CRWD","PANW","SHOP","MRVL","ANET",
]


# --- Backtest Config -----------------------------------------------------------

DEFAULT_TICKERS     = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]
DEFAULT_DAYS        = 30
DEFAULT_INTERVAL    = "5m"
STARTING_CAPITAL    = 10_000.0
RISK_PER_TRADE_PCT  = 0.01      # 1% of account per trade (intraday sizing)
TAKE_PROFIT_PCT     = 0.30
STOP_LOSS_PCT       = 0.25
MAX_HOLD_BARS       = 9         # ~45 min at 5m bars
COMMISSION_PER_LEG  = 0.65      # $ per contract per leg (typical retail)
SLIPPAGE_PCT        = 0.02      # 2% slippage on fill (wide spread for 0DTE)

# Minimum bars between entries on the same ticker
MIN_BARS_BETWEEN_ENTRIES = 3


# --- Black-Scholes ATM Approximation ------------------------------------------

def _bs_atm_delta_gamma(spot: float, iv: float, dte_days: float) -> tuple[float, float]:
    """
    Approximate delta and gamma for an ATM call using Black-Scholes.
    dte_days: fractional days to expiry.
    Returns (delta, gamma).
    """
    if dte_days <= 0 or iv <= 0 or spot <= 0:
        return 0.5, 0.0

    t = dte_days / 365.0
    sqrt_t = math.sqrt(t)

    # ATM: S ≈ K, so d1 ≈ 0.5 * iv * sqrt_t
    d1 = 0.5 * iv * sqrt_t

    # Standard normal pdf/cdf approximations
    def _norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

    delta = 0.5 + _norm_pdf(d1) * d1   # Simplified; ATM delta ≈ 0.50–0.55
    delta = max(0.1, min(0.9, delta))

    gamma = _norm_pdf(d1) / (spot * iv * sqrt_t) if (spot * iv * sqrt_t) > 0 else 0.0

    return delta, gamma


def _estimate_option_pnl(
    entry_stock: float,
    exit_stock: float,
    iv: float,
    dte_days: float,
    direction: str,
    quantity: int,
    entry_mid: float,
) -> float:
    """
    Estimate option P&L from a stock move using delta-gamma approximation.

    direction: "bullish" → call, "bearish" → put.
    entry_mid: option premium paid at entry (per share).
    Returns total P&L in dollars (negative = loss).
    """
    delta, gamma = _bs_atm_delta_gamma(entry_stock, iv, dte_days)

    stock_move = exit_stock - entry_stock
    if direction == "bearish":
        stock_move = -stock_move   # Put profits on down moves

    # Delta-gamma P&L (per share)
    option_move = delta * stock_move + 0.5 * gamma * stock_move ** 2

    # Clamp: max gain is unbounded but entry price is max loss per share
    option_move = max(-entry_mid, option_move)

    # Per contract: 100 shares
    pnl_per_contract = option_move * 100
    gross_pnl = pnl_per_contract * quantity

    # Costs
    commission = COMMISSION_PER_LEG * quantity * 2   # open + close
    slippage   = entry_mid * 100 * quantity * SLIPPAGE_PCT

    return round(gross_pnl - commission - slippage, 2)


def _get_hist_iv(close: pd.Series, window: int = 20) -> float:
    """Annualised HV from last `window` bars of close prices."""
    if len(close) < window + 1:
        return 0.30   # Default 30% vol if insufficient data
    log_ret = np.log(close / close.shift(1)).dropna()
    daily_std = float(log_ret.tail(window).std())
    # Intraday bars need different annualisation
    # For 5m bars: 78 bars/day → sqrt(252 * 78)
    bars_per_day = 78  # approx for 5m
    return daily_std * math.sqrt(252 * bars_per_day)


# --- Single-Ticker Backtest ----------------------------------------------------

def backtest_ticker(
    ticker: str,
    days: int = DEFAULT_DAYS,
    interval: str = DEFAULT_INTERVAL,
    starting_capital: float = STARTING_CAPITAL,
    min_score: int = MIN_SIGNAL_SCORE,
    show_trades: bool = False,
) -> dict:
    """
    Run the HFT backtest for a single ticker over `days` calendar days.

    Returns a results dict with P&L, win rate, and trade log.
    """
    period = f"{days}d"
    df = fetch_intraday(ticker, interval=interval, period=period)

    if df.empty or len(df) < 30:
        print(f"[Backtest] {ticker} — insufficient intraday data")
        return _empty_result(ticker)

    equity    = starting_capital
    trades    = []
    last_entry_bar = -MIN_BARS_BETWEEN_ENTRIES  # Allow entry from first bar

    print(f"\n[Backtest] {ticker} | {len(df)} bars | {interval} | {days}d")

    # Skip inverse ETFs entirely
    if ticker.upper() in INVERSE_ETF_LIST:
        print(f"[Backtest] {ticker} — skipped (inverse ETF)")
        return _empty_result(ticker)

    for i in range(30, len(df) - MAX_HOLD_BARS):
        # Cooldown between entries
        if i - last_entry_bar < MIN_BARS_BETWEEN_ENTRIES:
            continue

        # Gate: prime session only — no pre/post-market or EOD entries
        bar_time = df.index[i]
        if not is_prime_session(bar_time):
            continue

        window = df.iloc[:i + 1]
        vwap   = compute_vwap(window)

        # Determine direction before scoring (needed for trend modifier)
        orb_result  = detect_orb_breakout(window)
        vwap_result = detect_vwap_reclaim(window, vwap)
        orb_dir     = orb_result.get("direction", "none")
        vwap_sig    = vwap_result.get("signal", False)
        direction   = orb_dir if orb_dir in ("bullish", "bearish") else (
            "bullish" if vwap_sig else "bullish"
        )

        signals = {
            "vwap":     vwap_result,
            "orb":      orb_result,
            "spike":    detect_volume_spike(window),
            "momentum": detect_momentum_burst(window),
            "trend":    detect_trend_alignment(window),
        }

        score = score_hft_setup(signals, direction=direction)
        if score < min_score:
            continue

        entry_bar    = df.iloc[i]
        entry_price  = float(entry_bar["Close"])
        entry_time   = df.index[i]

        # Option premium: roughly 1–2% of stock for 0-DTE ATM
        # Use a simple model: premium = 0.5% of stock price for ultra-short DTE
        atm_premium  = entry_price * 0.005   # ~0.5% of stock
        atm_premium  = max(0.10, atm_premium)   # Floor at $0.10

        # Position sizing
        budget           = equity * RISK_PER_TRADE_PCT
        cost_per_contract = atm_premium * 100
        quantity = max(1, int(budget // cost_per_contract))

        # Historical IV for delta-gamma model
        iv = _get_hist_iv(window["Close"])

        # DTE assumption: 0.5 days (0-DTE, mid-session)
        dte_days = 0.5

        # Simulate forward bars for exit
        exit_bar_idx = None
        exit_reason  = "time_stop"
        exit_price   = float(df.iloc[min(i + MAX_HOLD_BARS, len(df) - 1)]["Close"])

        for j in range(1, MAX_HOLD_BARS + 1):
            if i + j >= len(df):
                break

            future_bar  = df.iloc[i + j]
            future_close = float(future_bar["Close"])

            # Estimate option value at this point
            option_move_est = (
                (future_close - entry_price) if direction == "bullish"
                else (entry_price - future_close)
            )
            option_pct = option_move_est / (entry_price * 0.005) if entry_price * 0.005 > 0 else 0

            if option_pct >= TAKE_PROFIT_PCT:
                exit_price   = future_close
                exit_bar_idx = i + j
                exit_reason  = "take_profit"
                break

            if option_pct <= -STOP_LOSS_PCT:
                exit_price   = future_close
                exit_bar_idx = i + j
                exit_reason  = "stop_loss"
                break

        exit_time = df.index[exit_bar_idx if exit_bar_idx else min(i + MAX_HOLD_BARS, len(df) - 1)]

        pnl = _estimate_option_pnl(
            entry_stock=entry_price,
            exit_stock=exit_price,
            iv=iv,
            dte_days=dte_days,
            direction=direction,
            quantity=quantity,
            entry_mid=atm_premium,
        )

        equity = max(0, equity + pnl)
        last_entry_bar = i

        active_sigs = [k for k, v in signals.items() if v.get("signal", False)]

        trade = {
            "ticker":     ticker,
            "entry_time": entry_time,
            "exit_time":  exit_time,
            "direction":  direction,
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "quantity":    quantity,
            "pnl":         pnl,
            "exit_reason": exit_reason,
            "score":       score,
            "signals":     ", ".join(active_sigs),
        }
        trades.append(trade)

        if show_trades:
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(
                f"  {entry_time.strftime('%m/%d %H:%M')} -> {exit_time.strftime('%H:%M')} | "
                f"{direction:8s} | Score: {score:3d} | "
                f"${entry_price:.2f}->${exit_price:.2f} | {pnl_str:>8} | {exit_reason}"
            )

    if not trades:
        print(f"[Backtest] {ticker} — no trades triggered")
        return _empty_result(ticker)

    df_trades  = pd.DataFrame(trades)
    total_pnl  = round(df_trades["pnl"].sum(), 2)
    win_trades = df_trades[df_trades["pnl"] > 0]
    loss_trades = df_trades[df_trades["pnl"] <= 0]
    win_rate   = len(win_trades) / len(df_trades) * 100 if trades else 0
    avg_win    = float(win_trades["pnl"].mean()) if len(win_trades) else 0
    avg_loss   = float(loss_trades["pnl"].mean()) if len(loss_trades) else 0
    profit_factor = (
        abs(win_trades["pnl"].sum() / loss_trades["pnl"].sum())
        if loss_trades["pnl"].sum() != 0 else float("inf")
    )
    final_equity  = round(starting_capital + total_pnl, 2)
    total_return  = round((total_pnl / starting_capital) * 100, 2)

    result = {
        "ticker":         ticker,
        "total_trades":   len(trades),
        "win_trades":     len(win_trades),
        "loss_trades":    len(loss_trades),
        "win_rate_pct":   round(win_rate, 1),
        "total_pnl":      total_pnl,
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "profit_factor":  round(profit_factor, 2),
        "final_equity":   final_equity,
        "total_return_pct": total_return,
        "trades":         trades,
    }

    print(
        f"[Backtest] {ticker} | "
        f"Trades: {len(trades)} | "
        f"Win rate: {win_rate:.1f}% | "
        f"P&L: ${total_pnl:+.2f} | "
        f"Return: {total_return:+.2f}% | "
        f"PF: {profit_factor:.2f}"
    )

    return result


def _empty_result(ticker: str) -> dict:
    return {
        "ticker": ticker, "total_trades": 0, "win_trades": 0,
        "loss_trades": 0, "win_rate_pct": 0.0, "total_pnl": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
        "final_equity": STARTING_CAPITAL, "total_return_pct": 0.0,
        "trades": [],
    }


# --- Multi-Ticker Backtest -----------------------------------------------------

def run_backtest(
    tickers: list[str] = DEFAULT_TICKERS,
    days: int = DEFAULT_DAYS,
    interval: str = DEFAULT_INTERVAL,
    min_score: int = MIN_SIGNAL_SCORE,
    show_trades: bool = False,
    output_csv: bool = True,
    use_universe: bool = False,
    max_tickers: int | None = None,
) -> list[dict]:
    """
    Run the HFT backtest across multiple tickers and print a summary table.

    Args:
        tickers:       Explicit ticker list (ignored when use_universe=True).
        use_universe:  If True, fetch the full S&P 500 from Wikipedia.
        max_tickers:   Cap the universe size (useful for quick test runs).
    """
    if use_universe:
        tickers = fetch_sp500_tickers(max_tickers=max_tickers)
    elif max_tickers:
        tickers = tickers[:max_tickers]

    print("\n" + "=" * 70)
    print("  HFT BACKTEST  —  Strategy 3")
    if use_universe:
        print(f"  Universe: S&P 500  ({len(tickers)} tickers)")
    else:
        print(f"  Tickers: {', '.join(tickers)}")
    print(f"  Period: {days} days back  |  Interval: {interval}  |  "
          f"Min score: {min_score}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f}  |  "
          f"Risk/trade: {RISK_PER_TRADE_PCT:.0%}  |  "
          f"TP: +{TAKE_PROFIT_PCT:.0%}  SL: -{STOP_LOSS_PCT:.0%}")
    print("=" * 70)

    results = []
    all_trades = []
    total = len(tickers)
    _start_ts = datetime.datetime.now()

    for idx, ticker in enumerate(tickers, 1):
        sys.stdout.write(f"[{idx}/{total}] ")
        sys.stdout.flush()
        r = backtest_ticker(
            ticker=ticker,
            days=days,
            interval=interval,
            min_score=min_score,
            show_trades=show_trades,
        )
        results.append(r)
        all_trades.extend(r["trades"])

    # -- Aggregate summary ------------------------------------------------------
    elapsed = datetime.datetime.now() - _start_ts
    elapsed_str = str(elapsed).split(".")[0]   # drop microseconds

    print("\n" + "=" * 70)
    print("  BACKTEST SUMMARY")
    print(f"  Elapsed: {elapsed_str}  |  Tickers processed: {total}")
    print("=" * 70)
    print(
        f"\n  {'Ticker':<8} {'Trades':>7} {'Win%':>7} {'Avg Win':>9} "
        f"{'Avg Loss':>9} {'PF':>6} {'P&L':>10} {'Return':>8}"
    )
    print("  " + "-" * 68)

    active_results = [r for r in results if r["total_trades"] > 0]
    skipped_count  = total - len(active_results)

    for r in sorted(active_results, key=lambda x: x["total_pnl"], reverse=True):
        pf_str = f"{r['profit_factor']:.2f}" if r["profit_factor"] != float("inf") else "inf"
        print(
            f"  {r['ticker']:<8} {r['total_trades']:>7} "
            f"{r['win_rate_pct']:>6.1f}% "
            f"${r['avg_win']:>8.2f} "
            f"${r['avg_loss']:>8.2f} "
            f"{pf_str:>6} "
            f"${r['total_pnl']:>+9.2f} "
            f"{r['total_return_pct']:>+7.2f}%"
        )

    if skipped_count:
        print(f"\n  ({skipped_count} tickers had 0 trades — omitted from table)")

    if all_trades:
        df_all    = pd.DataFrame(all_trades)
        total_pnl = round(df_all["pnl"].sum(), 2)
        total_n   = len(df_all)
        wins      = (df_all["pnl"] > 0).sum()
        overall_wr = wins / total_n * 100 if total_n else 0
        pf_all    = (
            abs(df_all[df_all["pnl"] > 0]["pnl"].sum() /
                df_all[df_all["pnl"] <= 0]["pnl"].sum())
            if df_all[df_all["pnl"] <= 0]["pnl"].sum() != 0
            else float("inf")
        )
        pf_str = f"{pf_all:.2f}" if pf_all != float("inf") else "inf"

        print("  " + "-" * 68)
        print(
            f"  {'ALL':<8} {total_n:>7} {overall_wr:>6.1f}% "
            f"{'':>9} {'':>9} {pf_str:>6} "
            f"${total_pnl:>+9.2f}"
        )

        # Exit reason breakdown
        reason_counts = df_all["exit_reason"].value_counts()
        print(f"\n  Exit reasons: " +
              " | ".join(f"{k}: {v}" for k, v in reason_counts.items()))

        # Signal effectiveness
        print("\n  Top signal combinations (>= 2 trades):")
        sig_perf = df_all.groupby("signals")["pnl"].agg(["count", "mean", "sum"])
        sig_perf = sig_perf[sig_perf["count"] >= 2].sort_values("sum", ascending=False)
        for sig, row in sig_perf.head(5).iterrows():
            print(
                f"    [{sig}] "
                f"n={int(row['count'])} | "
                f"avg ${row['mean']:+.2f} | "
                f"total ${row['sum']:+.2f}"
            )

        if output_csv:
            fname = f"backtest_hft_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            df_all.to_csv(fname, index=False)
            print(f"\n  Trade log saved to {fname}")

    print("\n" + "=" * 70 + "\n")
    return results


# --- CLI -----------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="HFT Backtest — Strategy 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtest_hft.py
  python backtest_hft.py --tickers TSLA NVDA AMD --days 60
  python backtest_hft.py --tickers AAPL --days 14 --show-trades
  python backtest_hft.py --interval 1m --min-score 60
  python backtest_hft.py --universe --days 60
  python backtest_hft.py --universe --max-tickers 100 --days 60 --show-trades
        """,
    )
    parser.add_argument("--tickers",      nargs="+", default=DEFAULT_TICKERS,
                        help="Tickers to backtest (default: SPY QQQ AAPL TSLA NVDA)")
    parser.add_argument("--universe",     action="store_true",
                        help="Use full S&P 500 universe fetched from Wikipedia")
    parser.add_argument("--max-tickers",  type=int, default=None,
                        help="Cap the number of tickers (useful with --universe for quick runs)")
    parser.add_argument("--days",         type=int, default=DEFAULT_DAYS,
                        help=f"Lookback in calendar days (default {DEFAULT_DAYS})")
    parser.add_argument("--interval",     default=DEFAULT_INTERVAL,
                        choices=["1m", "2m", "5m"],
                        help=f"Bar interval (default {DEFAULT_INTERVAL})")
    parser.add_argument("--min-score",    type=int, default=MIN_SIGNAL_SCORE,
                        help=f"Minimum signal score (default {MIN_SIGNAL_SCORE})")
    parser.add_argument("--show-trades",  action="store_true",
                        help="Print every individual trade during backtest")
    parser.add_argument("--no-csv",       action="store_true",
                        help="Skip saving trade log CSV")
    args = parser.parse_args()

    run_backtest(
        tickers=args.tickers,
        days=args.days,
        interval=args.interval,
        min_score=args.min_score,
        show_trades=args.show_trades,
        output_csv=not args.no_csv,
        use_universe=args.universe,
        max_tickers=args.max_tickers,
    )
