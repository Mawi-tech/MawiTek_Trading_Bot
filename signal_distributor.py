"""
signal_distributor.py — fan out sanitized signals to MANY servers/channels.

WHY THIS EXISTS
---------------
event_notifier.py sends alerts to ONE Discord webhook and ONE Telegram chat —
it reads DISCORD_WEBHOOK_URL / TELEGRAM_CHAT_ID once at import. That's fine for
the owner's private alert channel, but a *signal service* needs to broadcast the
same recommendation to many destinations (several Discord servers, several
Telegram groups) at once, each with its own filtering.

This module is that fan-out layer. It takes the account-agnostic PUBLIC feed
(signal_publisher.py) and delivers it to every configured target, tracking what
each target has already received so nothing is double-posted across runs.

SECURITY MODEL (read before changing anything)
----------------------------------------------
The bot trades the OWNER's real account. Subscribers must NEVER see account data
— equity, dollar P&L, position sizes, balances, broker keys. That boundary is
owned by signal_publisher.sanitize(), which FAILS CLOSED (raises on any
forbidden field). This module NEVER builds its own signals: it consumes only the
already-sanitized public feed, and re-runs sanitize() on every record
immediately before it leaves the process. A leak can't reach a subscriber
without first raising here.

    closed_trades.json  ──sanitize──►  public feed  ──sanitize again──►  targets
       (private)         (fail-closed)   (public)      (defense-in-depth)

CONFIG (distribution_targets.json — gitignored, holds webhook URLs / tokens)
----------------------------------------------------------------------------
    {
      "targets": [
        {
          "id": "discord-alpha",           // stable, unique — dedup key
          "type": "discord",
          "webhook_url": "https://discord.com/api/webhooks/...",
          "enabled": true,
          "strategies": ["pead", "iv_rank"],   // [] or omitted = all
          "min_setup_score": 60,               // 0 = no floor
          "min_conviction": "medium"           // low|medium|high (omit = any)
        },
        {
          "id": "telegram-vip",
          "type": "telegram",
          "bot_token": "123456:ABC-DEF...",
          "chat_id": "-1001234567890",
          "enabled": true
        }
      ]
    }

Copy distribution_targets.example.json → distribution_targets.json and fill it
in. With no file present the distributor is a safe no-op (just logs guidance).

RUN
---
    python signal_distributor.py            # one broadcast pass, then exit
    python signal_distributor.py --dry-run  # show what WOULD send, send nothing
    python signal_distributor.py --watch 60 # rebuild + broadcast every 60s
    python signal_distributor.py --status   # list targets + how many sent each
    python signal_distributor.py --test     # send a test message to every target
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from logger import get_logger
from state_io import read_json, update_json
from signal_publisher import build_public_feed, sanitize

log = get_logger("signal_distributor")

# Config: the list of destinations (secrets → gitignored, like .env).
TARGETS_FILE = "distribution_targets.json"
# Dedup ledger: per-target set of signal ids already delivered (machine-local).
STATE_FILE = "distribution_state.json"
# Cap the remembered-id history per target so the ledger can't grow unbounded.
MAX_SENT_PER_TARGET = 1000

_VALID_TYPES = {"discord", "telegram"}
# Conviction ranking for the optional min_conviction filter.
_CONVICTION_RANK = {"low": 1, "medium": 2, "high": 3}

# Discord embed colour per direction (mirrors event_notifier's palette intent).
_COLOUR_BULLISH = 0x169B6B   # green
_COLOUR_BEARISH = 0xD03C3C   # red
_COLOUR_NEUTRAL = 0x2B7AD4   # blue


# ─── Target config loading + validation ──────────────────────────────────────

def load_targets() -> list[dict]:
    """
    Read + validate the distribution targets. Returns a list of well-formed
    target dicts (invalid ones are dropped with a warning). Never raises; an
    absent/malformed file yields an empty list (→ the distributor no-ops).
    """
    raw = read_json(TARGETS_FILE, default={}) or {}
    targets = raw.get("targets") if isinstance(raw, dict) else None
    if not isinstance(targets, list):
        return []

    clean: list[dict] = []
    seen_ids: set[str] = set()
    for t in targets:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id", "")).strip()
        ttype = str(t.get("type", "")).strip().lower()
        if not tid or ttype not in _VALID_TYPES:
            log.warning("[distributor] skipping target with bad id/type: %r", t.get("id"))
            continue
        if tid in seen_ids:
            log.warning("[distributor] duplicate target id %r — skipping the second", tid)
            continue
        # Required credentials per type.
        if ttype == "discord" and not str(t.get("webhook_url", "")).strip():
            log.warning("[distributor] discord target %r missing webhook_url", tid)
            continue
        if ttype == "telegram" and not (str(t.get("bot_token", "")).strip()
                                        and str(t.get("chat_id", "")).strip()):
            log.warning("[distributor] telegram target %r missing bot_token/chat_id", tid)
            continue
        seen_ids.add(tid)
        clean.append(t)
    return clean


def _target_enabled(t: dict) -> bool:
    return bool(t.get("enabled", True))


# ─── Per-target filtering ─────────────────────────────────────────────────────

def signal_passes_filter(sig: dict, target: dict) -> bool:
    """
    True if a (sanitized) signal clears this target's filters. Filters are all
    optional; an omitted filter means "no restriction".
    """
    strategies = target.get("strategies")
    if isinstance(strategies, list) and strategies:
        if sig.get("strategy") not in strategies:
            return False

    min_score = target.get("min_setup_score")
    if isinstance(min_score, (int, float)) and min_score > 0:
        score = sig.get("setup_score")
        if not isinstance(score, (int, float)) or score < min_score:
            return False

    min_conv = target.get("min_conviction")
    if isinstance(min_conv, str) and min_conv.lower() in _CONVICTION_RANK:
        floor = _CONVICTION_RANK[min_conv.lower()]
        have = _CONVICTION_RANK.get(str(sig.get("conviction", "")).lower(), 0)
        if have < floor:
            return False

    return True


# ─── Message formatting (sanitized signal → human text) ───────────────────────

def _headline(sig: dict) -> str:
    """Top line: ticker, strategy, and CLOSED result or NEW-setup tag."""
    ticker = sig.get("ticker", "?")
    strat = sig.get("strategy", "?")
    ttype = sig.get("trade_type") or ""
    scope = f" ({ttype})" if ttype else ""
    result = sig.get("result_pct")
    if isinstance(result, (int, float)):
        emoji = "🟢" if result >= 0 else "🔴"
        tag = f"CLOSED {result:+.1f}%"
    else:
        emoji = "🔔"
        tag = "NEW SETUP"
    return f"{emoji} {ticker} — {strat}{scope}  [{tag}]"


def _detail_lines(sig: dict) -> list[str]:
    """Body lines. Percent + recommendation only — never dollars or sizes."""
    lines: list[str] = []

    struct_bits = []
    if sig.get("direction"):
        struct_bits.append(str(sig["direction"]))
    if sig.get("structure"):
        struct_bits.append(str(sig["structure"]))
    if sig.get("strike") is not None:
        struct_bits.append(f"strike {sig['strike']}")
    if sig.get("dte") is not None:
        struct_bits.append(f"{sig['dte']} DTE")
    if struct_bits:
        lines.append(" · ".join(struct_bits))

    meta = []
    if sig.get("conviction"):
        meta.append(f"conviction {sig['conviction']}")
    if sig.get("setup_score") is not None:
        meta.append(f"setup score {sig['setup_score']}")
    if meta:
        lines.append(" · ".join(meta))

    if sig.get("rationale"):
        lines.append(f"Why: {sig['rationale']}")

    lines.append("Signal only — not financial advice. Trade your own risk.")
    return lines


def _direction_colour(sig: dict) -> int:
    d = str(sig.get("direction", "")).lower()
    if d == "bullish":
        return _COLOUR_BULLISH
    if d == "bearish":
        return _COLOUR_BEARISH
    return _COLOUR_NEUTRAL


# ─── Low-level sends (parameterized — one per destination, not per-env) ───────

def _post_discord(webhook_url: str, sig: dict) -> bool:
    """Send one signal to a Discord webhook as an embed. Returns success."""
    embed = {
        "title":       _headline(sig),
        "description": "\n".join(_detail_lines(sig)),
        "color":       _direction_colour(sig),
        "footer":      {"text": "MawiTek — signal feed"},
    }
    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # Discord/Cloudflare reject urllib's default UA (403 / error 1010).
            "User-Agent": "MawiTek-Bot (https://github.com/mawitek, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300   # Discord returns 204 on success
    except urllib.error.HTTPError as e:
        log.error("[distributor:discord] HTTP %s: %s", e.code,
                  e.read().decode(errors="replace")[:200])
        return False
    except Exception as e:
        log.error("[distributor:discord] %s", e)
        return False


def _post_telegram(bot_token: str, chat_id: str, sig: dict) -> bool:
    """Send one signal to a Telegram chat as plain text. Returns success."""
    text = _headline(sig) + "\n" + "\n".join(_detail_lines(sig))
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read()).get("ok", False))
    except Exception as e:
        log.error("[distributor:telegram] %s", e)
        return False


def _send_to_target(target: dict, sig: dict) -> bool:
    """Dispatch one sanitized signal to one target by type."""
    if target["type"] == "discord":
        return _post_discord(target["webhook_url"], sig)
    if target["type"] == "telegram":
        return _post_telegram(target["bot_token"], target["chat_id"], sig)
    return False


# ─── Dedup ledger ─────────────────────────────────────────────────────────────

def _already_sent(target_id: str) -> set[str]:
    state = read_json(STATE_FILE, default={}) or {}
    sent = state.get("sent", {}) if isinstance(state, dict) else {}
    ids = sent.get(target_id, []) if isinstance(sent, dict) else []
    return set(ids) if isinstance(ids, list) else set()


def _record_sent(target_id: str, new_ids: list[str]) -> None:
    """Append delivered ids to the target's ledger (locked + atomic, bounded)."""
    if not new_ids:
        return

    def _mutate(state):
        if not isinstance(state, dict):
            state = {}
        sent = state.get("sent")
        if not isinstance(sent, dict):
            sent = {}
        existing = sent.get(target_id, [])
        if not isinstance(existing, list):
            existing = []
        # Preserve order, append only genuinely-new ids, keep the tail bounded.
        have = set(existing)
        for sid in new_ids:
            if sid not in have:
                existing.append(sid)
                have.add(sid)
        sent[target_id] = existing[-MAX_SENT_PER_TARGET:]
        state["sent"] = sent
        return state

    update_json(STATE_FILE, _mutate, default={})


# ─── Core broadcast ───────────────────────────────────────────────────────────

def broadcast(signals: list[dict] | None = None, dry_run: bool = False) -> dict:
    """
    Deliver every not-yet-sent signal to every enabled target that accepts it.

    `signals` defaults to the sanitized public feed (build_public_feed). Each
    record is sanitize()-checked again right before send (fail-closed). Dedup is
    per (target, signal-id), so re-running only sends what's new.

    Returns a per-target summary: {target_id: {"sent": n, "skipped": n, "failed": n}}.
    """
    if signals is None:
        signals = build_public_feed().get("signals", [])

    targets = [t for t in load_targets() if _target_enabled(t)]
    summary: dict[str, dict] = {}

    if not targets:
        log.info("[distributor] no enabled targets — nothing to do "
                 "(create %s from the .example)", TARGETS_FILE)
        return summary

    for target in targets:
        tid = target["id"]
        already = _already_sent(tid)
        result = {"sent": 0, "skipped": 0, "failed": 0}
        delivered_ids: list[str] = []

        for sig in signals:
            sid = sig.get("id")
            if not sid or sid in already:
                continue
            if not signal_passes_filter(sig, target):
                result["skipped"] += 1
                continue

            # Defense-in-depth: never let a record that isn't provably clean
            # leave the process. sanitize() RAISES on any forbidden field.
            safe = sanitize(sig)

            if dry_run:
                log.info("[distributor] (dry-run) would send %s → %s", sid, tid)
                result["sent"] += 1
                continue

            if _send_to_target(target, safe):
                result["sent"] += 1
                delivered_ids.append(sid)
            else:
                result["failed"] += 1

        if not dry_run:
            _record_sent(tid, delivered_ids)
        summary[tid] = result
        log.info("[distributor] %s → sent=%d skipped=%d failed=%d%s",
                 tid, result["sent"], result["skipped"], result["failed"],
                 " (dry-run)" if dry_run else "")

    return summary


def watch(interval: float = 60.0) -> None:
    """Rebuild the feed and broadcast new signals every `interval` seconds."""
    log.info("[distributor] watch mode — broadcasting every %.0fs (Ctrl+C to stop)",
             interval)
    while True:
        try:
            broadcast()
        except Exception as e:
            log.error("[distributor] broadcast pass failed: %s", e)
        time.sleep(max(5.0, interval))


# ─── Status + test helpers (CLI) ──────────────────────────────────────────────

def status() -> None:
    targets = load_targets()
    if not targets:
        print(f"No targets configured. Copy distribution_targets.example.json → "
              f"{TARGETS_FILE} and fill in your Discord webhooks / Telegram chats.")
        return
    print(f"{len(targets)} target(s) configured:\n")
    for t in targets:
        sent = len(_already_sent(t["id"]))
        state = "enabled" if _target_enabled(t) else "DISABLED"
        filt = []
        if t.get("strategies"):
            filt.append("strategies=" + ",".join(t["strategies"]))
        if t.get("min_setup_score"):
            filt.append(f"min_score={t['min_setup_score']}")
        if t.get("min_conviction"):
            filt.append(f"min_conviction={t['min_conviction']}")
        filt_s = ("  [" + " · ".join(filt) + "]") if filt else ""
        print(f"  {t['id']:20} {t['type']:9} {state:9} sent={sent}{filt_s}")


def send_test() -> None:
    """Send one clearly-labelled test signal to every enabled target."""
    test_sig = sanitize({
        "id": f"sig_test_{int(time.time())}", "status": "closed",
        "ticker": "TEST", "strategy": "diagnostic", "trade_type": "swing",
        "direction": "bullish", "structure": "long_call", "strike": 100,
        "dte": 30, "conviction": "high", "setup_score": 99,
        "rationale": "distributor connectivity test", "result_pct": 0.0,
    })
    targets = [t for t in load_targets() if _target_enabled(t)]
    if not targets:
        print(f"No enabled targets. Create {TARGETS_FILE} first.")
        return
    print(f"Sending a test signal to {len(targets)} target(s)...")
    for t in targets:
        ok = _send_to_target(t, test_sig)
        print(f"  {t['id']:20} {'✓ sent' if ok else '✗ FAILED — check creds/logs'}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fan out sanitized signals to multiple Discord/Telegram targets.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would send without sending or recording.")
    parser.add_argument("--watch", nargs="?", const=60.0, type=float, metavar="SECS",
                        help="Loop: rebuild + broadcast every SECS seconds (default 60).")
    parser.add_argument("--status", action="store_true",
                        help="List configured targets and how many signals each has received.")
    parser.add_argument("--test", action="store_true",
                        help="Send a labelled test signal to every enabled target.")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.test:
        send_test()
    elif args.watch is not None:
        watch(args.watch)
    else:
        result = broadcast(dry_run=args.dry_run)
        total = sum(r["sent"] for r in result.values())
        print(f"Broadcast complete — {total} signal-send(s) across "
              f"{len(result)} target(s).{' (dry-run)' if args.dry_run else ''}")
