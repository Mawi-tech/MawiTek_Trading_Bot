"""
bot_control.py — runtime control commands for the trading bot.

A small, dependency-free control surface that an EXTERNAL operator (your separate
Discord bot, a CLI, a script) can drive to steer the RUNNING trading bot without
being part of it. State lives in control_state.json; every strategy's
pre_trade_check honours it, so changes take effect on the next scan cycle — no
restart needed.

Commands
--------
    halt(reason)          block ALL new entries (a MANUAL halt — kept separate
                          from the daily-loss auto-halt, which resume() never
                          clears, so you can't accidentally un-halt a real breach)
    resume()              clear the manual halt
    pause_strategy(name)  block new entries for ONE strategy
    resume_strategy(name) unpause one strategy
    flatten(reason)       EMERGENCY: cancel orders + market-close everything
                          (delegates to kill_switch) and set the manual halt
    status()              current control state

Exposed over HTTP via dashboard_server's `POST /api/control` (so a separate bot
can call it), and runnable as a CLI:  python bot_control.py <cmd> [args]

control_block_reason() is the one function the trading bot itself calls (from
pre_trade_check). It FAILS OPEN — a missing/corrupt control file never blocks
trading.
"""

from __future__ import annotations

import os

from state_io import read_json, atomic_write_json, file_lock

try:
    from user_config import KNOWN_STRATEGIES as _KNOWN
    KNOWN_STRATEGIES = set(_KNOWN)
except Exception:   # keep usable even if user_config is unavailable
    KNOWN_STRATEGIES = {"catalyst_long_call", "iv_rank", "hft_intraday", "pead", "bounce"}

CONTROL_FILE = "control_state.json"


def _default() -> dict:
    return {"manual_halt": False, "halt_reason": "", "paused_strategies": []}


def load_control() -> dict:
    """Current control state, normalized + sanitized. Defaults when no file."""
    st = read_json(CONTROL_FILE, default=None)
    out = _default()
    if isinstance(st, dict):
        out["manual_halt"] = bool(st.get("manual_halt", False))
        out["halt_reason"] = str(st.get("halt_reason", "") or "")
        out["paused_strategies"] = sorted(
            {s for s in (st.get("paused_strategies") or []) if s in KNOWN_STRATEGIES}
        )
        if st.get("updated_at"):
            out["updated_at"] = st["updated_at"]
    return out


def _save(mutator) -> dict:
    """Locked read-modify-write of the control file."""
    with file_lock(CONTROL_FILE):
        st = load_control()
        mutator(st)
        try:
            from utils import now_est
            st["updated_at"] = now_est().isoformat(timespec="seconds")
        except Exception:
            pass
        atomic_write_json(CONTROL_FILE, st)
        return st


# ─── commands ───────────────────────────────────────────────────────────────────

def halt(reason: str = "manual") -> dict:
    def m(st):
        st["manual_halt"] = True
        st["halt_reason"] = str(reason)[:200]
    return _save(m)


def resume() -> dict:
    def m(st):
        st["manual_halt"] = False
        st["halt_reason"] = ""
    return _save(m)


def pause_strategy(name: str) -> dict:
    name = (name or "").strip()
    if name not in KNOWN_STRATEGIES:
        raise ValueError(f"unknown strategy '{name}' (known: {', '.join(sorted(KNOWN_STRATEGIES))})")

    def m(st):
        ps = set(st.get("paused_strategies") or [])
        ps.add(name)
        st["paused_strategies"] = sorted(ps)
    return _save(m)


def resume_strategy(name: str) -> dict:
    name = (name or "").strip()

    def m(st):
        ps = set(st.get("paused_strategies") or [])
        ps.discard(name)
        st["paused_strategies"] = sorted(ps)
    return _save(m)


def status() -> dict:
    return load_control()


def flatten(reason: str = "manual") -> dict:
    """
    EMERGENCY: cancel all open orders, market-close every broker position, and set
    the manual halt. Delegates to kill_switch.flatten_all (the existing kill
    switch). Returns the per-position close result.
    """
    from kill_switch import flatten_all
    result = flatten_all(reason=reason, set_halt_flag=True)
    halt(f"flatten: {reason}")
    return {"flattened": result}


# ─── the hook the trading bot calls ─────────────────────────────────────────────

def control_block_reason(strategy: str | None) -> str | None:
    """
    Reject reason if the bot is manually halted or `strategy` is paused, else None.
    FAILS OPEN — a missing/unreadable control file never blocks trading.
    """
    try:
        st = load_control()
    except Exception:
        return None
    if st.get("manual_halt"):
        return f"Manually halted ({st.get('halt_reason') or 'manual'})"
    if strategy and strategy in (st.get("paused_strategies") or []):
        return f"Strategy '{strategy}' paused"
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    argv = sys.argv[1:]
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "halt":
            res = halt(argv[1] if len(argv) > 1 else "manual")
        elif cmd == "resume":
            res = resume()
        elif cmd == "pause":
            res = pause_strategy(argv[1])
        elif cmd in ("unpause", "resume-strategy"):
            res = resume_strategy(argv[1])
        elif cmd == "status":
            res = status()
        elif cmd == "flatten":
            if "--confirm" not in argv:
                print("Refusing: flatten closes ALL positions. Re-run with --confirm.")
                sys.exit(2)
            res = flatten(reason="cli")
        else:
            print(f"Unknown command '{cmd}'. Use: halt|resume|pause <s>|unpause <s>|status|flatten --confirm")
            sys.exit(2)
        print(json.dumps(res, indent=2))
    except (IndexError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(2)
