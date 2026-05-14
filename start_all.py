"""
start_all.py  —  MawiTek Trading Bot launcher

Boots every component the bot needs as separate subprocesses, streams their
output to per-process log files, and shuts them all down cleanly on Ctrl+C.

What it starts:
    1. dashboard_server.py    — serves the live HTML dashboard on :8000
    2. executor.py            — Strategy 1 (swing options, 7–30 DTE)
    3. iv_rank_bot.py         — Strategy 2 (IV-rank premium plays)
    4. hft_executor.py        — Strategy 3 (intraday 0-DTE momentum)

Each bot has its own internal market-hours guard, so it's safe to start them
24/7 — they'll idle outside market hours and resume automatically at the
open. You'll see the dashboard fill in as live data arrives.

Usage:
    python start_all.py                         # start everything
    python start_all.py --no-dashboard          # skip dashboard server
    python start_all.py --only executor         # run just one bot
    python start_all.py --only executor hft_executor
    python start_all.py --logs-dir logs         # change log destination
"""

from __future__ import annotations

import argparse
import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ─── Component registry ─────────────────────────────────────────────────────────
# Order matters: dashboard first so the bots can write to it as they start up.

COMPONENTS: dict[str, dict] = {
    "dashboard": {
        "script":  "dashboard_server.py",
        "args":    ["--no-browser"],
        "label":   "Dashboard server",
        "needs_broker": False,
    },
    "executor": {
        "script":  "executor.py",
        "args":    [],
        "label":   "Strategy 1 — Swing options executor",
        "needs_broker": True,
    },
    "iv_rank_bot": {
        "script":  "iv_rank_bot.py",
        "args":    [],
        "label":   "Strategy 2 — IV rank bot",
        "needs_broker": True,
    },
    "hft_executor": {
        "script":  "hft_executor.py",
        "args":    [],
        "label":   "Strategy 3 — Intraday HFT executor",
        "needs_broker": True,
    },
}


# ─── Helpers ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.resolve()


def _check_env_ready() -> bool:
    """Quick pre-flight: make sure .env is present and broker creds look set."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        print(f"[start_all] ERROR: {env_path} not found.")
        print("            Run sandbox_validator.py first to configure credentials.")
        return False

    # Read .env without importing the bot — keeps this script independent.
    content = env_path.read_text(encoding="utf-8", errors="replace")
    has_key  = "TRADIER_API_KEY=" in content and "PASTE_YOUR" not in content.split("TRADIER_API_KEY=")[1].split("\n")[0]
    has_acct = "TRADIER_ACCOUNT_ID=" in content and "PASTE_YOUR" not in content.split("TRADIER_ACCOUNT_ID=")[1].split("\n")[0]

    if not (has_key and has_acct):
        print("[start_all] WARNING: TRADIER credentials look like placeholders in .env.")
        print("            Bots that need a broker will run in MOCK_MODE only.")
        return True   # not fatal — dashboard still works

    return True


def _spawn(name: str, comp: dict, logs_dir: Path) -> subprocess.Popen:
    """Launch one component as a subprocess with stdout/stderr -> log file."""
    script = ROOT / comp["script"]
    if not script.exists():
        raise FileNotFoundError(f"{name}: {script} missing — cannot start")

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{name}.log"
    log_fp   = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

    log_fp.write(
        f"\n\n{'='*70}\n"
        f"  Started {name} at {datetime.datetime.now().isoformat()}\n"
        f"{'='*70}\n\n"
    )
    log_fp.flush()

    cmd = [sys.executable, str(script), *comp["args"]]

    # On Windows we want a new process group so Ctrl+C only stops us
    # (not the children), then we can signal them cleanly ourselves.
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    proc._mawitek_log_fp = log_fp  # type: ignore[attr-defined]
    proc._mawitek_log_path = log_path  # type: ignore[attr-defined]
    return proc


def _stop(proc: subprocess.Popen, name: str, timeout: float = 5.0) -> None:
    """Politely ask a subprocess to stop, then kill if it ignores us."""
    if proc.poll() is not None:
        return

    print(f"[start_all] Stopping {name} (pid {proc.pid})...", flush=True)

    try:
        if sys.platform.startswith("win"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except Exception as e:
        print(f"[start_all]   signal failed ({e}); forcing kill")
        proc.kill()
        return

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[start_all]   {name} did not stop in {timeout}s — killing")
        proc.kill()

    fp = getattr(proc, "_mawitek_log_fp", None)
    if fp:
        try: fp.close()
        except Exception: pass


# ─── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Boot all MawiTek bot components together.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--only",       nargs="+", metavar="NAME",
                        choices=list(COMPONENTS.keys()),
                        help="Run only the named component(s). Default: all.")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Skip the dashboard HTTP server.")
    parser.add_argument("--logs-dir",   default="logs",
                        help="Where to write per-component log files (default: ./logs).")
    args = parser.parse_args()

    if not _check_env_ready():
        return 1

    # Decide what to run
    if args.only:
        selected = list(args.only)
    else:
        selected = list(COMPONENTS.keys())
        if args.no_dashboard and "dashboard" in selected:
            selected.remove("dashboard")

    logs_dir = (ROOT / args.logs_dir).resolve()

    print("=" * 70)
    print("  MAWITEK BOT LAUNCHER")
    print(f"  Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Logs:    {logs_dir}")
    print(f"  Running: {', '.join(selected)}")
    print("=" * 70)
    print()

    procs: dict[str, subprocess.Popen] = {}

    try:
        for name in selected:
            comp = COMPONENTS[name]
            try:
                proc = _spawn(name, comp, logs_dir)
                procs[name] = proc
                print(f"  [OK]  {comp['label']:<48} pid={proc.pid}  log={proc._mawitek_log_path.name}")
                # Brief stagger so the dashboard server is listening before
                # the bots start writing state.
                time.sleep(0.4)
            except Exception as e:
                print(f"  [FAIL] {name}: {e}")

        if not procs:
            print("\n[start_all] Nothing started. Exiting.")
            return 1

        print()
        print("-" * 70)
        print(f"  All components launched.  Dashboard: http://localhost:8000/dashboard.html")
        print(f"  Tail any log: type \"{logs_dir}\\<name>.log\"")
        print(f"  Press Ctrl+C to stop everything.")
        print("-" * 70)

        # Babysit: poll every second, restart-aware reporting on crashes.
        while True:
            time.sleep(1.0)
            for name, proc in list(procs.items()):
                rc = proc.poll()
                if rc is not None:
                    log_path = getattr(proc, "_mawitek_log_path", None)
                    print(
                        f"\n[start_all] WARNING: {name} exited with code {rc}. "
                        f"Last 20 lines of its log:"
                    )
                    if log_path and log_path.exists():
                        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        for ln in lines[-20:]:
                            print(f"    | {ln}")
                    print(f"[start_all] {name} will NOT auto-restart. "
                          f"Fix the issue, then re-run start_all.py.")
                    del procs[name]

            if not procs:
                print("\n[start_all] All components have exited. Quitting.")
                break

    except KeyboardInterrupt:
        print("\n\n[start_all] Ctrl+C received — shutting down...")

    finally:
        # Reverse order so dashboard stops last (it doesn't depend on bots).
        for name in reversed(list(procs.keys())):
            _stop(procs[name], name)
        print("[start_all] All components stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
