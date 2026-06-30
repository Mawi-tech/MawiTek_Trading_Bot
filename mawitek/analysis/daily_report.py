"""
daily_report.py — end-of-day summary pushed to your notification channels.

Gathers the day's activity and sends one digest via event_notifier:

    • Realized P&L today and account equity
    • Trades closed today: count, win/loss, best/worst
    • Open positions carried into tomorrow (overnight exposure)
    • Whether the daily-loss halt fired
    • Lifetime profit factor / win rate for context

Trigger it three ways:

    1. On demand:                 python daily_report.py
    2. Scheduled (Task Scheduler / cron) at ~4:05pm ET
    3. Automatically: the swing executor calls maybe_send_eod_summary() when
       it first goes idle after the close — deduped to once per trading day.
"""

from __future__ import annotations

import datetime

from mawitek.infra.utils import today_est
from mawitek.infra.state_io import read_json, atomic_write_json, file_lock
from mawitek.infra.logger import get_logger

log = get_logger("daily_report")

MARKER_FILE = "last_summary.json"   # remembers the last date we sent, for dedup


# ─── Gather ───────────────────────────────────────────────────────────────────

def _today_str() -> str:
    return today_est().isoformat()


def gather_daily_stats() -> dict:
    """Collect everything the summary needs. Pure reads, no side effects."""
    from mawitek.core.trade_journal import load_closed_trades
    from mawitek.analysis.analytics_metrics import compute_metrics
    from mawitek.core.equity_tracker import load_equity_curve
    from mawitek.core.risk_manager import load_state

    today = _today_str()
    all_trades = load_closed_trades()
    today_trades = [
        t for t in all_trades
        if str(t.get("exit_time", "")).startswith(today)
    ]

    pnls = [float(t.get("pnl_dollar", 0) or 0) for t in today_trades]
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    realized = round(sum(pnls), 2)
    best = round(max(pnls), 2) if pnls else 0.0
    worst = round(min(pnls), 2) if pnls else 0.0

    # Open positions carried overnight
    try:
        from mawitek.core.position_manager import load_positions
        open_positions = load_positions()
    except Exception:
        open_positions = {}
    overnight_cost = sum(
        float(d.get("entry_price", 0) or 0) * abs(int(d.get("quantity", 0) or 0)) * 100
        for d in open_positions.values()
    )

    # HFT positions (should be flat after EOD, but report if not)
    hft_open = 0
    hft = read_json("hft_positions.json", [])
    if isinstance(hft, list):
        hft_open = len(hft)

    # IV-rank positions (multi-day holds — expected to carry overnight)
    iv = read_json("iv_rank_positions.json", [])
    iv_open = len(iv) if isinstance(iv, list) else 0

    risk_state = load_state()
    curve = load_equity_curve()
    equity = float(curve[-1]["equity"]) if curve else 0.0

    lifetime = compute_metrics(curve, all_trades)

    return {
        "date": today,
        "equity": round(equity, 2),
        "realized_today": realized,
        "trades_today": len(today_trades),
        "wins_today": len(wins),
        "losses_today": len(losses),
        "best_today": best,
        "worst_today": worst,
        "halted": bool(risk_state.get("halted")),
        "halt_reason": risk_state.get("halt_reason"),
        "open_positions": len(open_positions),
        "overnight_cost": round(overnight_cost, 2),
        "hft_open": hft_open,
        "iv_open": iv_open,
        "lifetime_win_rate": lifetime["trades"].get("win_rate"),
        "lifetime_profit_factor": lifetime["trades"].get("profit_factor"),
        "lifetime_max_dd": lifetime["max_drawdown"].get("pct"),
    }


# ─── Format + send ────────────────────────────────────────────────────────────

def _format_lines(s: dict) -> list[str]:
    rp = s["realized_today"]
    rp_str = f"{'+' if rp >= 0 else '-'}${abs(rp):,.2f}"
    lines = [
        f"Date: {s['date']}",
        f"Equity: ${s['equity']:,.2f}",
        f"Realized P&L today: {rp_str}",
        f"Trades closed: {s['trades_today']}  (W {s['wins_today']} / L {s['losses_today']})",
    ]
    if s["trades_today"]:
        lines.append(f"Best: +${s['best_today']:,.0f}   Worst: -${abs(s['worst_today']):,.0f}")
    if s["halted"]:
        lines.append(f"⛔ Daily-loss halt FIRED today ({s.get('halt_reason') or 'limit hit'})")
    lines.append(f"Open overnight: {s['open_positions']} catalyst + {s.get('iv_open', 0)} IV-rank, "
                 f"${s['overnight_cost']:,.0f} catalyst cost basis")
    if s["hft_open"]:
        lines.append(f"⚠ {s['hft_open']} HFT position(s) still open — expected flat after EOD")
    pf = s["lifetime_profit_factor"]
    pf_str = "∞" if pf in (None, float("inf")) and s["lifetime_win_rate"] else (f"{pf}" if pf is not None else "n/a")
    lines.append(f"Lifetime: win rate {s['lifetime_win_rate']}% · profit factor {pf_str} · max DD {s['lifetime_max_dd']}%")
    return lines


def send_daily_summary() -> bool:
    """Build and dispatch the summary. Returns True if dispatched."""
    stats = gather_daily_stats()
    lines = _format_lines(stats)
    severity = "danger" if stats["halted"] else ("success" if stats["realized_today"] >= 0 else "warning")
    try:
        from mawitek.infra.event_notifier import _dispatch
        _dispatch(subject=f"Daily summary — {stats['date']}", lines=lines, severity=severity)
        log.info("Daily summary sent for %s", stats["date"])
        return True
    except Exception as e:
        log.error("Failed to send daily summary: %s", e)
        return False


def maybe_send_eod_summary() -> bool:
    """
    Send the summary at most once per trading day. Safe to call repeatedly
    (e.g. every idle cycle after the close) — it dedups via a marker file.
    Returns True only on the cycle that actually sends.
    """
    today = _today_str()
    with file_lock(MARKER_FILE):
        marker = read_json(MARKER_FILE, {})
        if marker.get("date") == today:
            return False
        # Claim today's slot before sending so concurrent strategies don't
        # both fire the summary.
        atomic_write_json(MARKER_FILE, {"date": today, "sent_at": datetime.datetime.now().isoformat()})
    return send_daily_summary()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send the end-of-day summary.")
    parser.add_argument("--once", action="store_true",
                        help="Only send if not already sent today (deduped).")
    parser.add_argument("--print", action="store_true",
                        help="Print the stats/summary instead of sending.")
    args = parser.parse_args()

    if args.print:
        s = gather_daily_stats()
        print("\n".join(_format_lines(s)))
    elif args.once:
        sent = maybe_send_eod_summary()
        print("Sent." if sent else "Already sent today — skipped.")
    else:
        send_daily_summary()
        print("Summary dispatched (check your channels, or logs if none configured).")
