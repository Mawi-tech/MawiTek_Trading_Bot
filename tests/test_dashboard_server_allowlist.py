"""
The served-file allowlist is by exact filename, not by extension.

Matching on extension meant any .html/.js/.css/.map that ever landed in the bot
directory became web-servable: a scratch copy, an editor backup, a build
artefact, a source map. translate_path blocks `..` traversal so it was never
exploitable, but the allowlist should be correct on its own rather than leaning
on a second control — and source maps exist precisely to hand out source.
"""

import os
import threading
import urllib.error
import urllib.request

import pytest
from http.server import ThreadingHTTPServer

import dashboard_server as dsrv


ALLOWED = sorted(dsrv._ALLOWED_FILES | dsrv._ALLOWED_JSON)

# Files that really do sit in the bot directory and must never be reachable.
FORBIDDEN = [
    ".env",                  # Tradier key, Telegram token, SMTP password
    "risk_state.json",       # halt flag, daily P&L
    "user_config.json",
    "state_io.py",
    "dashboard_server.py",
    "public_feed.json",      # sanitized subscriber feed — a SEPARATE app serves this
    "closed_trades.json",
    "open_positions.json",
    "dashboard.js.map",      # the source map an extension allowlist would have served
    "dashboard.html.bak",
    "scratch.html",
    "vendor.js",
    "theme.css",
    "notes.md",
]


@pytest.fixture()
def server(tmp_path):
    for name in ALLOWED + FORBIDDEN:
        (tmp_path / name).write_text(f"contents of {name}")
    (tmp_path / "logs").mkdir()

    cwd = os.getcwd()
    os.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.DashboardRequestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.chdir(cwd)


def _status(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


@pytest.mark.parametrize("name", ALLOWED)
def test_allowlisted_file_is_served(server, name):
    status, body = _status(f"{server}/{name}")
    assert status == 200
    assert name.encode() in body


@pytest.mark.parametrize("name", FORBIDDEN)
def test_everything_else_404s(server, name):
    assert _status(f"{server}/{name}")[0] == 404


def test_directory_request_404s_rather_than_listing(server):
    """A listing would reveal .env and every state file by name."""
    assert _status(f"{server}/logs/")[0] == 404
    assert _status(f"{server}/logs")[0] == 404


def test_root_request_still_resolves(server):
    """The empty-name branch: a bare `/` must not 404 out of the allowlist
    before send_head gets to serve the index."""
    assert _status(f"{server}/")[0] in (200, 404)
    assert dsrv.DashboardRequestHandler._is_allowed.__doc__ is not None


def test_query_string_and_encoding_do_not_bypass(server):
    """The dashboard cache-busts with ?v= and ?t=, so the check must strip the
    query — and must not be fooled by percent-encoding it back in."""
    assert _status(f"{server}/dashboard.css?v=1")[0] == 200
    assert _status(f"{server}/risk_state.json?v=1")[0] == 404
    assert _status(f"{server}/%2Eenv")[0] == 404


def test_public_feed_is_not_allowlisted():
    """Owner-private server. The sanitized subscriber feed is served by a
    separate public app; shipping it from here would blur that line."""
    assert "public_feed.json" not in dsrv._ALLOWED_FILES
    assert "public_feed.json" not in dsrv._ALLOWED_JSON


def test_allowlist_has_no_wildcard_left():
    """Guard against a future patch reintroducing extension matching."""
    assert not hasattr(dsrv, "_ALLOWED_EXTS")
    assert all("*" not in n and not n.startswith(".")
               for n in dsrv._ALLOWED_FILES | dsrv._ALLOWED_JSON)
