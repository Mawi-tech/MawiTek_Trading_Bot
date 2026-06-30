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
import base64
import hmac
import json
import os
import posixpath
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

# Load .env so the optional dashboard password (DASH_AUTH_USER/PASS) is picked up.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

# ─── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PORT = 8000
DEFAULT_BIND = "127.0.0.1"  # loopback — local-only by default
DEFAULT_PAGE = "dashboard.html"
BROWSER_OPEN_DELAY_SEC = 0.6  # give the server a moment to start listening

# Optional HTTP Basic Auth — defense-in-depth for when the dashboard is exposed
# beyond loopback (e.g. over Tailscale or a tunnel). Off unless BOTH are set.
_AUTH_USER = os.getenv("DASH_AUTH_USER", "").strip()
_AUTH_PASS = os.getenv("DASH_AUTH_PASS", "").strip()
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)

# ─── Served-file allowlist ─────────────────────────────────────────────────────
# CRITICAL: the bot directory also contains .env (API keys, Telegram token,
# SMTP password, Discord webhook) and machine-local state files. A plain static
# server would happily serve GET /.env — leaking every credential — and a GET /
# would list every filename. We restrict what can be fetched to exactly the
# dashboard assets and the JSON files the dashboards read. Everything else 404s.
_ALLOWED_EXTS = {".html", ".css", ".js", ".ico", ".png", ".svg", ".woff", ".woff2", ".map"}
# news_feed.json / social_sentiment.json are fetched directly by the News and
# Social tabs (their own fast polls, so a headline or sentiment update doesn't
# wait for the next dashboard_state write).
#
# ⚠️ OWNER-PRIVATE SURFACE — DO NOT EXPOSE TO SUBSCRIBERS.
# dashboard_state.json contains the owner's equity, dollar P&L, position SIZES,
# and drawdown $ — this server is for the owner only (loopback / Tailscale, with
# optional Basic auth). For a multi-user SIGNAL SERVICE, subscribers get the
# SANITIZED, account-agnostic feed from signal_publisher.py (public_feed.json,
# percentages only) served by a SEPARATE public app — never this server, and
# public_feed.json is deliberately NOT in this allowlist.
_ALLOWED_JSON = {"dashboard_state.json", "backtest_equity.json",
                 "news_feed.json", "social_sentiment.json"}

# The ONE write endpoint: the dashboard POSTs the user's risk-config edits here.
# The body is a tiny JSON blob ({tier, overrides}); cap it hard so a bad/hostile
# request can't stream megabytes at us. All validation/clamping happens in
# user_config.save_user_config — the server just gatekeeps and forwards.
CONFIG_ENDPOINT = "/api/config"
# Runtime control for an external operator (your separate Discord bot): halt /
# resume / pause-strategy / status / flatten. Same auth + body cap as config.
CONTROL_ENDPOINT = "/api/control"
MAX_CONFIG_BODY = 64 * 1024   # 64 KB


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
        # Cache-busting — the dashboard HTML also uses ?t=Date.now() but these
        # headers make the behaviour bulletproof across browsers and proxies.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # Security hardening — the server is loopback-only by default, but
        # these headers add defence-in-depth against clickjacking and MIME sniffing.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        super().end_headers()

    # ── Access control ──────────────────────────────────────────────────────
    def _is_allowed(self) -> bool:
        """Allow only dashboard assets and the specific JSON the pages read."""
        raw_path = urlsplit(self.path).path           # strip ?query
        name = posixpath.basename(unquote(raw_path))
        # Root or bare directory request → handled by send_head (we serve the
        # dashboard, never a listing — see list_directory below).
        if name == "":
            return True
        ext = posixpath.splitext(name)[1].lower()
        if ext in _ALLOWED_EXTS:
            return True
        if name in _ALLOWED_JSON:
            return True
        return False

    # ── Optional password (HTTP Basic Auth) ─────────────────────────────────
    def _authorized(self) -> bool:
        """True if auth is off, or the request carries the right Basic creds."""
        if not _AUTH_ENABLED:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
        except Exception:
            return False
        # constant-time compare to avoid leaking length/content via timing
        return hmac.compare_digest(user, _AUTH_USER) and hmac.compare_digest(pw, _AUTH_PASS)

    def _send_auth_challenge(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MawiTek Dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_head(self):
        # Single chokepoint for both GET and HEAD in SimpleHTTPRequestHandler.
        if not self._authorized():
            self._send_auth_challenge()
            return None
        if not self._is_allowed():
            self.send_error(404, "Not Found")
            return None
        return super().send_head()

    def list_directory(self, path):
        # Never expose a directory listing — it would reveal .env and every
        # state file by name. Serve the dashboard instead of a listing.
        self.send_error(404, "Not Found")
        return None

    # ── Config save endpoint (POST /api/config) ─────────────────────────────
    def do_POST(self) -> None:
        """
        Persist dashboard-edited risk config. The only mutating route.

        Same auth gate as GET (so an exposed dashboard with DASH_AUTH set is
        write-protected too), a hard body-size cap, strict JSON parsing, and the
        actual clamping/validation delegated to user_config.save_user_config.
        Loopback-only binding remains the first line of defence against CSRF.
        """
        if not self._authorized():
            self._send_auth_challenge()
            return

        path = urlsplit(self.path).path
        if path not in (CONFIG_ENDPOINT, CONTROL_ENDPOINT):
            self.send_error(404, "Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        if length > MAX_CONFIG_BODY:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        if path == CONFIG_ENDPOINT:
            self._handle_config(payload)
        else:
            self._handle_control(payload)

    def _handle_config(self, payload: dict) -> None:
        try:
            import user_config
            saved = user_config.save_user_config(payload)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"could not save: {e}"})
            return
        print(f"[config] Saved risk config: tier={saved.get('tier')} "
              f"overrides={list(saved.get('overrides', {}).keys())}", flush=True)
        self._send_json(200, {"ok": True, "saved": saved})

    def _handle_control(self, payload: dict) -> None:
        """Dispatch a runtime control command (halt/resume/pause/status/flatten)."""
        action = str(payload.get("action", "")).strip().lower()
        try:
            import bot_control as bc
            if action == "halt":
                state = bc.halt(payload.get("reason", "manual"))
            elif action == "resume":
                state = bc.resume()
            elif action == "pause":
                state = bc.pause_strategy(payload.get("strategy", ""))
            elif action in ("unpause", "resume_strategy"):
                state = bc.resume_strategy(payload.get("strategy", ""))
            elif action == "status":
                state = bc.status()
            elif action == "flatten":
                # The one that closes positions — require an explicit confirm token.
                if payload.get("confirm") != "FLATTEN":
                    self._send_json(400, {"ok": False, "error": "flatten requires confirm=FLATTEN"})
                    return
                state = bc.flatten(payload.get("reason", "manual"))
            else:
                self._send_json(400, {"ok": False, "error": f"unknown action '{action}'"})
                return
        except ValueError as e:                      # e.g. unknown strategy name
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        print(f"[control] action={action} state={state}", flush=True)
        self._send_json(200, {"ok": True, "action": action, "state": state})

    def _send_json(self, code: int, obj: dict) -> None:
        # Close the connection after replying so any unread request body (e.g. an
        # oversized POST we rejected before reading) is discarded with the socket.
        self.close_connection = True
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


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
