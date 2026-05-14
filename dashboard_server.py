"""
MawiTek Dashboard Server
========================

Serves the bot's HTML dashboards (dashboard.html, backtest_dashboard.html) and
their JSON state files over HTTP so the browser's fetch() calls actually work.

WHY THIS EXISTS
---------------
Opening dashboard.html directly as a file:// URL fails: modern browsers block
fetch() against the local filesystem for security. We need a tiny HTTP server
sitting in front of the same directory.

USAGE
-----
From the bot directory:
    python dashboard_server.py

Then open in your browser:
    http://localhost:8000/dashboard.html
    http://localhost:8000/backtest_dashboard.html

OPTIONS
-------
    --port N        Listen on a different port (default 8000)
    --dir PATH      Serve a different directory (default: current dir)
    --no-browser    Don't auto-open the dashboard in the browser
    --bind HOST     Bind to a specific interface (default 127.0.0.1, loopback only)

SAFETY
------
Binds to 127.0.0.1 (loopback) by default — only your local machine can reach it.
The dashboards never expose credentials, but there's still no reason to make
your bot state accessible to the LAN. Use --bind 0.0.0.0 explicitly if you
really do want LAN access (e.g. viewing the dashboard from your phone).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ─── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PORT = 8000
DEFAULT_BIND = "127.0.0.1"  # loopback — local-only by default
DEFAULT_PAGE = "dashboard.html"
BROWSER_OPEN_DELAY_SEC = 0.6  # give the server a moment to start listening


# ─── Custom request handler ────────────────────────────────────────────────────

class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """
    Static file server with two tweaks:
      1. Aggressive no-cache headers on every response so dashboard JSON updates
         show up immediately without forcing the user to hard-reload.
      2. Cleaner log lines than the default stderr spam.
    """

    # Disable the default per-request log so we can format our own
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches base class
        # Skip favicon noise (browsers ask for it; we don't have one)
        if isinstance(args, tuple) and len(args) >= 1 and "/favicon.ico" in str(args[0]):
            return
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {self.address_string()} - {format % args}", flush=True)

    def end_headers(self) -> None:
        # Defense-in-depth: the dashboard HTML already cache-busts via ?t=Date.now(),
        # but adding these headers makes the no-cache behavior bulletproof across
        # browsers and proxies.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_serve_dir(cli_dir: str | None) -> str:
    """
    Pick the directory to serve. Priority:
      1. --dir argument
      2. Directory containing this script (so the user can run from anywhere)
    """
    if cli_dir:
        path = os.path.abspath(cli_dir)
    else:
        path = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isdir(path):
        print(f"ERROR: Serve directory does not exist: {path}", file=sys.stderr)
        sys.exit(2)

    return path


def _check_expected_files(serve_dir: str) -> None:
    """
    Warn (don't fail) if the dashboard files aren't where we expect them.
    Helps catch the 'ran it from the wrong folder' mistake immediately.
    """
    expected = ["dashboard.html", "backtest_dashboard.html"]
    missing = [f for f in expected if not os.path.isfile(os.path.join(serve_dir, f))]

    if missing:
        print(f"WARNING: Could not find {missing} in {serve_dir}")
        print("         The server will still run, but the dashboards may 404.")
        print("         Either cd into your bot folder before running, or pass --dir.\n")


def _open_browser_after_delay(url: str, delay: float = BROWSER_OPEN_DELAY_SEC) -> None:
    """Open the dashboard in the default browser shortly after server start."""
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001 - non-fatal
            print(f"[browser] Could not auto-open browser: {e}")

    threading.Thread(target=_open, daemon=True).start()


def _build_server(bind: str, port: int) -> ThreadingHTTPServer:
    """
    Construct the HTTP server, translating port-conflict errors into a
    clear, actionable message instead of a Python traceback.
    """
    try:
        return ThreadingHTTPServer((bind, port), DashboardRequestHandler)
    except OSError as e:
        if e.errno in (98, 10048):  # 98=EADDRINUSE (Linux), 10048=WSAEADDRINUSE (Windows)
            print(f"\nERROR: Port {port} is already in use.")
            print("       Either stop whatever is using it, or pick a different port:")
            print(f"           python dashboard_server.py --port {port + 1}\n")
        elif e.errno in (13, 10013):  # permission denied
            print(f"\nERROR: Permission denied binding to {bind}:{port}.")
            print("       Ports below 1024 typically require admin/root. Try --port 8000 or higher.\n")
        else:
            print(f"\nERROR: Could not start server on {bind}:{port}: {e}\n")
        sys.exit(1)


# ─── Main entry point ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Static file server for the MawiTek bot dashboards.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python dashboard_server.py                       # serve current dir on port 8000\n"
            "  python dashboard_server.py --port 8080           # different port\n"
            "  python dashboard_server.py --no-browser          # don't auto-open browser\n"
            "  python dashboard_server.py --dir A:\\bot\\         # serve a specific folder\n"
            "  python dashboard_server.py --bind 0.0.0.0        # allow LAN access (use cautiously)\n"
        )
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--dir", dest="serve_dir", type=str, default=None,
                        help="Directory to serve (default: directory containing this script)")
    parser.add_argument("--bind", type=str, default=DEFAULT_BIND,
                        help=f"Network interface to bind (default: {DEFAULT_BIND}, loopback only)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open the dashboard in the default browser")
    parser.add_argument("--page", type=str, default=DEFAULT_PAGE,
                        help=f"Page to open in the browser (default: {DEFAULT_PAGE})")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    serve_dir = _resolve_serve_dir(args.serve_dir)
    os.chdir(serve_dir)  # SimpleHTTPRequestHandler serves from cwd

    _check_expected_files(serve_dir)

    server = _build_server(args.bind, args.port)

    # Build the URL the user should actually visit. If they bound to 0.0.0.0,
    # show them the loopback URL anyway — it's the most useful one.
    display_host = "localhost" if args.bind in ("0.0.0.0", "127.0.0.1") else args.bind
    dashboard_url = f"http://{display_host}:{args.port}/{args.page}"
    backtest_url = f"http://{display_host}:{args.port}/backtest_dashboard.html"

    print("=" * 60)
    print("  MawiTek Dashboard Server")
    print("=" * 60)
    print(f"  Serving:   {serve_dir}")
    print(f"  Bind:      {args.bind}:{args.port}")
    print(f"  Dashboard: {dashboard_url}")
    print(f"  Backtest:  {backtest_url}")
    print(f"  Press Ctrl+C to stop.")
    print("=" * 60 + "\n")

    if not args.no_browser:
        _open_browser_after_delay(dashboard_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down...")
    finally:
        server.server_close()
        print("[server] Stopped.")


if __name__ == "__main__":
    main()
