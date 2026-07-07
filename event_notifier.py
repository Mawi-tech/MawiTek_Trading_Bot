"""
event_notifier.py — push notifications for important bot events.

Fires on the things you actually need to know about right now — a fill
happened, a halt was triggered, a position is moving fast. These are
infrequent and high-signal, routed to Telegram / email / Discord.

Public API
----------
    notify_trade_filled(strategy, ticker, contract, qty, price, cost)
    notify_position_closed(ticker, contract, pnl_dollar, pnl_pct, reason)
    notify_halt_triggered(equity, pnl, limit)
    notify_big_move(ticker, contract, pnl_pct, entry_price, current_price)
    notify_trade_setups(setups, style, strategy)   # day/swing candidate heads-up

Each one is a no-op (just logs) when no channel is configured, so you
can sprinkle calls into the bot without worrying about a missing env var.

Config (.env)
-------------
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
    DISCORD_WEBHOOK_URL
    ALERT_SMS_TO   — carrier email-to-SMS gateway addr, e.g. 5551234567@vtext.com
                     (needs the SMTP_* vars; texts urgent events only unless
                     SMS_ALL_EVENTS=true)

Verify with:  python event_notifier.py          (sends a test to each channel)
              python event_notifier.py --status  (just shows what's configured)

Big-move dedup
--------------
    notify_big_move() throttles per (ticker, contract, direction-of-move-bucket)
    so a position that crosses the threshold once doesn't spam every cycle.
    State is in-memory only — restart clears the dedup, which is intentional
    (you do want to know on restart if a position is still in extreme territory).
"""

from __future__ import annotations

import datetime
import json
import os
import smtplib
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText

from logger import get_logger
from state_io import atomic_write_json, read_json

log = get_logger("event_notifier")

# Load .env BEFORE reading any channel vars, so `python event_notifier.py`
# (run standalone, without importing tradier_client) still sees the credentials.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
except ImportError:
    pass   # python-dotenv not installed — fall back to OS env vars only

# Rolling on-disk feed of recent events so the dashboard can show them even when
# no notification channel (Telegram/Discord/email) is configured.
EVENTS_FILE = "events.json"
EVENTS_MAX  = 50


def _log_event(subject: str, lines: list[str], severity: str) -> None:
    """Append an event to the rolling events.json feed. Never raises."""
    try:
        event = {
            "ts":       time.time(),
            "iso":      datetime.datetime.now().isoformat(timespec="seconds"),
            "subject":  subject,
            "summary":  " · ".join(lines),
            "severity": severity,
        }
        data = read_json(EVENTS_FILE, [])
        if not isinstance(data, list):
            data = []
        data.append(event)
        atomic_write_json(EVENTS_FILE, data[-EVENTS_MAX:])
    except Exception:
        pass  # the feed is best-effort; never block a notification on it


# ─── Config (read once at import) ─────────────────────────────────────────────

_TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID",   "").strip()

_SMTP_HOST  = os.getenv("SMTP_HOST",          "").strip()
_SMTP_PORT  = int(os.getenv("SMTP_PORT",      "587"))
_SMTP_USER  = os.getenv("SMTP_USER",          "").strip()
_SMTP_PASS  = os.getenv("SMTP_PASSWORD",      "").strip()
_FROM_ADDR  = os.getenv("ALERT_EMAIL_FROM",   _SMTP_USER).strip()
_TO_ADDRS   = [a.strip() for a in os.getenv("ALERT_EMAIL_TO", "").split(",") if a.strip()]

_DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
# Optional per-Discord event-type filter. Comma-separated list of kinds to send
# to Discord ONLY (other channels are unaffected). Empty = send everything.
# Kinds: fill, close, halt, big_move, setup.  e.g. DISCORD_EVENT_KINDS=setup
_DISCORD_KINDS = {k.strip() for k in os.getenv("DISCORD_EVENT_KINDS", "").split(",") if k.strip()}

# SMS via the carrier's email-to-SMS gateway — reuses SMTP, no extra account.
# Set ALERT_SMS_TO to the gateway address(es), e.g. "5551234567@vtext.com"
# (Verizon), "@txt.att.net" (AT&T), "@tmomail.net" (T-Mobile). Comma-separate.
# By default SMS only fires for urgent events (fills/closes/halts/big moves),
# not the frequent setup heads-ups; set SMS_ALL_EVENTS=true to text everything.
_SMS_ADDRS = [a.strip() for a in os.getenv("ALERT_SMS_TO", "").split(",") if a.strip()]
_SMS_ALL   = os.getenv("SMS_ALL_EVENTS", "").strip().lower() in ("1", "true", "yes")


_TG_ENABLED      = bool(_TG_TOKEN and _TG_CHAT)
_EMAIL_ENABLED   = bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASS and _TO_ADDRS)
_DISCORD_ENABLED = bool(_DISCORD_URL)
_SMS_ENABLED     = bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASS and _SMS_ADDRS)
_ANY_ENABLED     = _TG_ENABLED or _EMAIL_ENABLED or _DISCORD_ENABLED or _SMS_ENABLED


# Discord embed colour palette per severity
_COLOURS = {
    "info":    0x2B7AD4,   # blue
    "success": 0x169B6B,   # green
    "warning": 0xC98315,   # amber
    "danger":  0xD03C3C,   # red
}


# ─── Low-level channel sends ──────────────────────────────────────────────────

def _send_telegram(text: str) -> bool:
    """Send a plain-text Telegram message."""
    if not _TG_ENABLED:
        return False
    url     = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": _TG_CHAT, "text": text}).encode()
    req     = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = json.loads(resp.read()).get("ok", False)
        if not ok:
            log.warning("[event:telegram] API returned ok=false")
        return bool(ok)
    except Exception as e:
        log.error("[event:telegram] %s", e)
        return False


def _send_email(subject: str, body: str) -> bool:
    if not _EMAIL_ENABLED:
        return False
    msg            = MIMEText(body, "plain")
    msg["Subject"] = "[MawiTek] " + subject
    msg["From"]    = _FROM_ADDR
    msg["To"]      = ", ".join(_TO_ADDRS)
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(_SMTP_USER, _SMTP_PASS)
            srv.sendmail(_FROM_ADDR, _TO_ADDRS, msg.as_string())
        return True
    except Exception as e:
        log.error("[event:email] %s", e)
        return False


def _send_sms(subject: str, body: str) -> bool:
    """
    Text a SHORT alert to a phone via the carrier email-to-SMS gateway (reuses
    SMTP). Carriers cap length and split long texts, so we send one compact line.
    """
    if not _SMS_ENABLED:
        return False
    first = body.split("\n", 1)[0] if body else ""
    text  = f"{subject} | {first}".strip(" |")[:150]
    msg            = MIMEText(text, "plain")
    msg["Subject"] = ""   # gateways prepend the subject — keep it clean
    msg["From"]    = _FROM_ADDR
    msg["To"]      = ", ".join(_SMS_ADDRS)
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(_SMTP_USER, _SMTP_PASS)
            srv.sendmail(_FROM_ADDR, _SMS_ADDRS, msg.as_string())
        return True
    except Exception as e:
        log.error("[event:sms] %s", e)
        return False


def _send_discord(title: str, lines: list[str], severity: str = "info") -> bool:
    if not _DISCORD_ENABLED:
        return False
    embed = {
        "title":       title,
        "description": "\n".join(lines),
        "color":       _COLOURS.get(severity, _COLOURS["info"]),
        "footer":      {"text": "MawiTek — event notification"},
    }
    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        _DISCORD_URL,
        data=payload,
        # Discord/Cloudflare reject urllib's default UA (HTTP 403, error 1010),
        # so send an explicit User-Agent.
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MawiTek-Bot (https://github.com/mawitek, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Discord returns 204 No Content on success
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log.error("[event:discord] HTTP %s: %s", e.code, e.read().decode(errors="replace")[:200])
        return False
    except Exception as e:
        log.error("[event:discord] %s", e)
        return False


# ─── Core dispatch ────────────────────────────────────────────────────────────

def _dispatch(subject: str, lines: list[str], severity: str = "info",
              kind: str = "general") -> None:
    """Fan out to every enabled channel. Never raises.

    `kind` (fill/close/halt/big_move/setup) is used only to honour the optional
    DISCORD_EVENT_KINDS filter — other channels always receive every event.
    """
    # Always record to the dashboard feed first, regardless of channels.
    _log_event(subject, lines, severity)

    body = "\n".join(lines)
    if not _ANY_ENABLED:
        log.info("[event] (no channels) %s | %s", subject, body.replace("\n", " · "))
        return
    text = f"{subject}\n\n{body}"
    _send_telegram(text)
    _send_email(subject, body)
    if not _DISCORD_KINDS or kind in _DISCORD_KINDS:
        _send_discord(subject, lines, severity)
    # SMS is intrusive — by default only for urgent events (non-"info"), unless
    # SMS_ALL_EVENTS is set. Setup heads-ups are "info" → no texts by default.
    if _SMS_ALL or severity != "info":
        _send_sms(subject, body)


# ─── Hold-duration helpers ────────────────────────────────────────────────────

# Expected holding horizon per strategy, derived from each executor's actual
# time-stop / exit rules (kept in sync with the hft/pead/bounce executors). Shown
# on ENTRY alerts so a subscriber knows how long the trade is meant to be held;
# on EXIT alerts we report the ACTUAL time held instead.
STRATEGY_HORIZON = {
    "hft_intraday":       "intraday — closed within ~60 min, flat by the bell",
    "catalyst_long_call": "swing — a few days (catalyst plays out)",
    "pead":               "swing — up to ~16 days (time-stop)",
    "bounce":             "swing — up to ~9 days (time-stop)",
    "vwap_fade":          "intraday — reverts to VWAP within ~30 min, flat by the bell",
}


def hold_horizon(strategy: str, style: str = "") -> str:
    """Human-readable expected hold for a strategy; falls back to the style."""
    horizon = STRATEGY_HORIZON.get(strategy)
    if horizon:
        return horizon
    return "intraday" if style == "day" else "swing — multiple days"


# Per-strategy exit plan — mirrors each executor's TAKE_PROFIT_PCT / STOP_LOSS_PCT
# / MAX_HOLD_* (KEEP IN SYNC if those change). Drives the concrete buy/sell/time
# plan on entry alerts. tp/sl are option-premium percentages; `days` is the
# calendar time-stop (0 = intraday/flat-by-close, None = held through the
# catalyst / to expiry). `mins` is the intraday time-stop for day strategies.
# Most strategies are long CALLS; vwap_fade is BIDIRECTIONAL, so the plan text is
# resolved from the setup's direction (see trade_plan).
STRATEGY_PLAN = {
    "hft_intraday":       {"tp": 100, "sl": 20, "days": 0, "mins": 60},
    "catalyst_long_call": {"tp": 100, "sl": 50, "days": None},
    "pead":               {"tp": 80,  "sl": 35, "days": 16},
    "bounce":             {"tp": 60,  "sl": 35, "days": 9},
    "vwap_fade":          {"tp": 40,  "sl": 25, "days": 0, "mins": 30},
}


def _entry_ref_price(setup: dict):
    """Best available 'current price' from a setup to anchor the entry, or None."""
    # "last_price" is what the intraday scanners (hft / vwap_fade) emit; without
    # it the day-trade alerts showed no entry price at all.
    for k in ("as_of_close", "price", "last", "last_price", "underlying_price", "entry_price"):
        v = setup.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _fmt_price(p: float) -> str:
    return f"${p:,.2f}"


def hold_window(strategy: str) -> str:
    """Concise expected hold, derived from the strategy's plan (mins for intraday,
    days for swing). Complements the fuller STRATEGY_HORIZON sentence."""
    plan = STRATEGY_PLAN.get(strategy) or {}
    days = plan.get("days")
    if days == 0:
        return f"~{plan.get('mins', 60)} min (flat by the close)"
    if isinstance(days, int) and days > 0:
        return f"up to ~{days} day(s)"
    if days is None and plan:
        return "held through the catalyst / to expiry"
    return "intraday" if strategy in ("hft_intraday", "vwap_fade") else "multi-day"


def move_estimate(strategy: str, setup: dict) -> str:
    """
    One line describing the POTENTIAL MOVE for a setup: estimated entry, a concrete
    target level where the strategy defines one, the expected move size, and the
    hold window. Best-effort from the fields a setup already carries — returns ""
    if there's nothing useful to say.
    """
    parts: list[str] = []
    entry = _entry_ref_price(setup)
    if entry:
        parts.append(f"entry ~{_fmt_price(entry)}")

    # Concrete underlying TARGET: the mean-reversion fade targets VWAP itself, so
    # we can quote the level and the expected reversion size in %.
    vwap = setup.get("vwap")
    if strategy == "vwap_fade" and entry and isinstance(vwap, (int, float)) and vwap > 0:
        move_pct = abs(entry - vwap) / entry * 100
        parts.append(f"target VWAP {_fmt_price(vwap)} (~{move_pct:.1f}% reversion)")
    elif isinstance(setup.get("pct_vs_vwap"), (int, float)):
        parts.append(f"{setup['pct_vs_vwap']:+.1f}% vs VWAP")

    parts.append(f"hold {hold_window(strategy)}")
    return " · ".join(parts)


def trade_plan(strategy: str, setup: dict) -> str:
    """
    One-line concrete buy/sell/time plan for an entry (long-call) alert:
    where to enter, the target/stop to sell at, and when the trade times out.
    """
    plan = STRATEGY_PLAN.get(strategy)
    price = _entry_ref_price(setup)
    near = f" near ${price:,.2f}" if price else ""
    # Direction-aware: bearish setups (e.g. a vwap_fade of a rip) buy PUTS.
    side = "puts" if setup.get("direction") == "bearish" else "calls"
    if not plan:
        return f"buy {side} now{near} — manage to target/stop per strategy"

    tp, sl, days = plan["tp"], plan["sl"], plan["days"]
    if days == 0:
        when = f"time-stop {plan.get('mins', 60)} min (flat by the close)"
    elif days is None:
        when = "hold through the catalyst (or option expiry)"
    else:
        try:
            import datetime as _dt
            from utils import today_est
            by = (today_est() + _dt.timedelta(days=days)).isoformat()
            when = f"time-stop ~{days}d (by {by})"
        except Exception:
            when = f"time-stop ~{days}d"
    return f"buy {side} now{near} · sell at +{tp}% target or -{sl}% stop · {when}"


def _format_held(entry_time, now=None) -> str:
    """
    Human hold duration from an ISO entry timestamp (or datetime) to `now`:
    days once past 24h, else hours/minutes. Empty string if unparseable/missing,
    so callers can simply omit the line. Handles both tz-aware and naive stamps.
    """
    import datetime as _dt
    if not entry_time:
        return ""
    try:
        et = (_dt.datetime.fromisoformat(entry_time)
              if isinstance(entry_time, str) else entry_time)
    except Exception:
        return ""
    if not isinstance(et, _dt.datetime):
        return ""
    if now is None:
        now = _dt.datetime.now(et.tzinfo) if et.tzinfo else _dt.datetime.now()
    try:
        secs = max(0, int((now - et).total_seconds()))
    except Exception:
        return ""   # naive/aware mismatch or other subtraction error
    days = secs // 86400
    if days >= 1:
        return f"{days}d"
    hrs = secs // 3600
    mins = (secs % 3600) // 60
    return f"{hrs}h {mins}m" if hrs else f"{mins}m"


# ─── Public event helpers ─────────────────────────────────────────────────────

def notify_trade_filled(strategy: str, ticker: str, contract: str,
                        qty: int, price: float, cost: float) -> None:
    """Order placed and filled by the broker."""
    _dispatch(
        subject  = f"Trade filled — {ticker} {contract}",
        lines    = [
            f"Strategy: {strategy}",
            f"Qty: {qty} @ ${price:,.2f}",
            f"Cost: ${cost:,.2f}",
        ],
        severity = "success",
        kind     = "fill",
    )


def notify_position_closed(ticker: str, contract: str,
                           pnl_dollar: float, pnl_pct: float, reason: str,
                           strategy: str = "", entry_time=None) -> None:
    """
    Position exited (TP, SL, expiry, time-stop, manual, external) — i.e. a SELL.

    Framed as an explicit SELL so it's unmistakable in the alert stream next to
    the scanner's BUY heads-ups. When `entry_time` is provided, reports how long
    the position was ACTUALLY held — the companion to the expected-hold horizon
    shown on the entry alert.
    """
    sign     = "+" if pnl_dollar >= 0 else "-"
    severity = "success" if pnl_dollar >= 0 else "danger"
    lines    = [
        f"P&L: {sign}${abs(pnl_dollar):,.2f} ({pnl_pct:+.1f}%)",
        f"Reason: {reason}",
    ]
    held = _format_held(entry_time)
    if held:
        lines.append(f"Held: {held}")
    if strategy:
        lines.append(f"Strategy: {strategy}")
    _dispatch(
        subject  = f"SELL {ticker} {contract} — closed {pnl_pct:+.1f}%",
        lines    = lines,
        severity = severity,
        kind     = "close",
    )


def notify_halt_triggered(equity: float, pnl: float, limit: float) -> None:
    """Daily-loss halt tripped — no more trades today."""
    pct = (pnl / equity * 100) if equity > 0 else 0.0
    _dispatch(
        subject  = "Daily loss limit hit — trading HALTED",
        lines    = [
            f"Equity: ${equity:,.2f}",
            f"Today's P&L: ${pnl:+,.2f} ({pct:+.2f}%)",
            f"Limit: ${limit:,.2f}",
            "Bot will not open new positions until tomorrow.",
        ],
        severity = "danger",
        kind     = "halt",
    )


# ─── Scanner setup alerts (subscriber heads-up, not an order) ─────────────────

# Only setups at or above this score alert by default — subscribers want the
# best few candidates, not every marginal blip. Callers may pass their own.
ALERT_SETUP_MIN_SCORE = 60
ALERT_SETUP_MAX_PER_MSG = 6

# Dedup: each (ticker, style) alerts at most once per ET trading day, so the
# 60-second HFT loop can't re-alert the same name every cycle. In-memory,
# per-process — a restart re-arms the alerts, which is acceptable.
_SETUP_ALERTED: set[tuple[str, str, str]] = set()


def _alert_prefs() -> dict:
    """
    Scanner-alert preferences from user_config (dashboard-set), with safe
    fallbacks so alerts behave exactly as before when no config is present.
    `strategies=None` in the fallback means "no per-strategy filter".
    """
    try:
        from user_config import alert_config
        return alert_config()
    except Exception:
        return {"enabled": True, "min_score": ALERT_SETUP_MIN_SCORE,
                "strategies": None, "watchlist": []}


def notify_trade_setups(setups: list[dict], style: str, strategy: str,
                        min_score: int | None = None) -> int:
    """
    Push a heads-up about fresh scanner setups worth a look — labelled DAY-TRADE
    or SWING so a subscriber knows the intended hold.

    Honours the dashboard-set alert prefs (user_config): a master on/off, the
    minimum score, and which strategies may alert. WATCHLIST tickers alert on ANY
    setup — below the score floor and regardless of the per-strategy toggle — as
    long as alerts are on; they're tagged "[watchlist]".

    Batched: one message per scan cycle (max ALERT_SETUP_MAX_PER_MSG names),
    deduped per (ticker, style) per ET day. Returns the number alerted. Never raises.
    """
    try:
        prefs = _alert_prefs()
        if not prefs.get("enabled", True):
            return 0
        allowed = prefs.get("strategies")                       # None → no filter
        strat_ok = (allowed is None) or (strategy in allowed)
        threshold = (prefs.get("min_score", ALERT_SETUP_MIN_SCORE)
                     if min_score is None else min_score)
        watch = {t.upper() for t in (prefs.get("watchlist") or [])}

        from utils import today_est
        day = today_est().isoformat()

        fresh: list[tuple[dict, bool]] = []
        for s in setups or []:
            ticker = s.get("ticker")
            if not ticker:
                continue
            on_watch = ticker.upper() in watch
            # Non-watchlist setups must clear BOTH the strategy filter and the
            # score floor; watchlist tickers bypass both (master switch still on).
            if not on_watch and (not strat_ok or s.get("setup_score", 0) < threshold):
                continue
            key = (ticker, style, day)
            if key in _SETUP_ALERTED:
                continue
            _SETUP_ALERTED.add(key)
            fresh.append((s, on_watch))
            if len(fresh) >= ALERT_SETUP_MAX_PER_MSG:
                break

        if not fresh:
            return 0

        label = "Day-trade" if style == "day" else "Swing"
        horizon = hold_horizon(strategy, style)
        any_watch = any(w for _, w in fresh)
        lines = []
        for s, on_watch in fresh:
            why = (s.get("style_reason")
                   or ", ".join(s.get("active_signals", []))
                   or strategy)
            direction = f" {s.get('direction')}" if s.get("direction") else ""
            tag = "[watchlist] " if on_watch else ""
            # "BUY" prefix so entry alerts read unmistakably as buys, next to the
            # SELL exit alerts from notify_position_closed. The indented Plan line
            # spells out where to enter, the sell target/stop, and the time-stop.
            lines.append(f"{tag}BUY {s['ticker']}: {s.get('setup_score')}/100{direction} — {why}")
            move = move_estimate(strategy, s)
            if move:
                lines.append(f"   Est. move: {move}")
            lines.append(f"   Plan: {trade_plan(strategy, s)}")
        lines.append(f"Expected hold: {horizon}")
        lines.append(f"Source: {strategy} scanner. Heads-up only — not an order.")

        subject = (f"{label} setups — {len(fresh)} new candidate(s)"
                   + (" (watchlist)" if any_watch else ""))
        _dispatch(subject=subject, lines=lines, severity="info", kind="setup")
        return len(fresh)
    except Exception as e:
        log.warning("notify_trade_setups failed: %s", e)
        return 0


# In-memory dedup so the same position doesn't fire every cycle.
# Key: (option_symbol, bucket_str)  where bucket_str is e.g. ">+20%", "<-20%"
_BIG_MOVE_SEEN: set[tuple[str, str]] = set()
_BIG_MOVE_THRESHOLD = 20.0  # percent


def notify_big_move(ticker: str, option_symbol: str, contract: str,
                    pnl_pct: float, entry_price: float, current_price: float) -> None:
    """
    Position has moved >= ±20% from entry. Fires once per crossing bucket
    so you get one alert when it first crosses +20%, one if it later
    crosses +40%, etc., but not on every cycle in between.
    """
    if abs(pnl_pct) < _BIG_MOVE_THRESHOLD:
        return

    # Bucket per 20% so we re-alert on each new threshold crossed
    bucket_n  = int(abs(pnl_pct) // _BIG_MOVE_THRESHOLD)
    direction = "+" if pnl_pct >= 0 else "-"
    key       = (option_symbol, f"{direction}{bucket_n}")

    if key in _BIG_MOVE_SEEN:
        return
    _BIG_MOVE_SEEN.add(key)

    severity = "success" if pnl_pct >= 0 else "warning"
    _dispatch(
        subject  = f"Big move — {ticker} {contract} {pnl_pct:+.1f}%",
        lines    = [
            f"Entry:   ${entry_price:.2f}",
            f"Current: ${current_price:.2f}",
            f"P&L:     {pnl_pct:+.1f}%",
        ],
        severity = severity,
        kind     = "big_move",
    )


# ─── Status helper (dashboard indicator + `python event_notifier.py`) ─────────

def channel_status() -> dict:
    """Which alert channels are configured. Embedded in dashboard state so the UI
    can show what's live without exposing any tokens."""
    return {
        "telegram": _TG_ENABLED,
        "email":    _EMAIL_ENABLED,
        "discord":  _DISCORD_ENABLED,
        "sms":      _SMS_ENABLED,
        "any":      _ANY_ENABLED,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Event notifier — config check + test send")
    parser.add_argument("--status", action="store_true",
                        help="Show channel config and exit (don't send a test)")
    args = parser.parse_args()

    st = channel_status()
    print("Event notifier configuration:")
    for ch in ("telegram", "email", "discord", "sms"):
        print(f"  {ch.capitalize():9} {'ENABLED' if st[ch] else 'disabled'}")

    if not st["any"]:
        print("\nNo channels configured. Edit .env and set at least one of:")
        print("  DISCORD_WEBHOOK_URL")
        print("  SMTP_HOST + SMTP_PORT + SMTP_USER + SMTP_PASSWORD + ALERT_EMAIL_FROM + ALERT_EMAIL_TO")
        print("  ALERT_SMS_TO=5551234567@vtext.com   (carrier email-to-SMS gateway; needs the SMTP_* vars too)")
    elif args.status:
        print("\n(status only — no test sent)")
    else:
        print("\nSending a test alert to each ENABLED channel...")
        results = {
            "telegram": _send_telegram("MawiTek test — notifier configured correctly."),
            "email":    _send_email("Test notification", "MawiTek event notifier configured correctly."),
            "discord":  _send_discord("Test notification",
                                      ["MawiTek event notifier configured correctly."], "info"),
            "sms":      _send_sms("MawiTek test", "Notifier configured correctly."),
        }
        for ch in ("telegram", "email", "discord", "sms"):
            if st[ch]:
                print(f"  {ch.capitalize():9} {'✓ sent' if results[ch] else '✗ FAILED — check creds/logs'}")
        print("\nDone. Check your phone / inbox / Discord.")
