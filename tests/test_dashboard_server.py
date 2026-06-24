"""
Integration test for the dashboard server's access control.

The bot directory contains .env (API keys, tokens, SMTP password). The server
must NEVER serve it, and must not expose a directory listing. It must still
serve the dashboard assets and the dashboard_state.json the page reads.
"""

import json
import os
import threading
import urllib.request
import urllib.error

import pytest
from http.server import ThreadingHTTPServer

import dashboard_server as dsrv


@pytest.fixture()
def server(tmp_path):
    # Build a fake bot dir with a secret and an allowed asset.
    (tmp_path / ".env").write_text("TRADIER_API_KEY=supersecret\n")
    (tmp_path / "risk_state.json").write_text('{"halted": false}')
    (tmp_path / "dashboard.html").write_text("<html>ok</html>")
    (tmp_path / "dashboard_state.json").write_text('{"ok": true}')

    cwd = os.getcwd()
    os.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.DashboardRequestHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
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


def test_env_is_blocked(server):
    status, _ = _status(server + "/.env")
    assert status == 404


def test_state_files_blocked(server):
    # Sensitive runtime state must not be downloadable.
    assert _status(server + "/risk_state.json")[0] == 404


def test_directory_listing_blocked(server):
    assert _status(server + "/")[0] == 404


def test_dashboard_html_served(server):
    status, body = _status(server + "/dashboard.html")
    assert status == 200
    assert b"ok" in body


def test_dashboard_state_served(server):
    status, body = _status(server + "/dashboard_state.json")
    assert status == 200
    assert b"ok" in body


def test_security_headers_present(server):
    with urllib.request.urlopen(server + "/dashboard.html", timeout=5) as r:
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"


# ── POST /api/config (the only write endpoint) ───────────────────────────────

def _post(url, payload, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_config_post_saves_and_clamps(server):
    status, body = _post(server + "/api/config",
                         {"tier": "auto",
                          "overrides": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 0}})
    assert status == 200
    j = json.loads(body)
    assert j["ok"] is True
    ov = j["saved"]["overrides"]
    assert ov["risk_per_trade_pct"] == 0.10      # clamped to the 10% ceiling
    assert ov["daily_loss_limit_pct"] == 0.01    # daily-loss halt can't be disabled
    assert os.path.exists("user_config.json")    # persisted into the served dir


def test_config_post_rejects_bad_json(server):
    assert _post(server + "/api/config", None, raw=b"{not valid json")[0] == 400


def test_config_post_wrong_path_404(server):
    assert _post(server + "/api/nope", {"tier": "auto"})[0] == 404


def test_config_post_body_too_large(server):
    big = b'{"tier":"auto","overrides":{}}' + b" " * (dsrv.MAX_CONFIG_BODY + 10)
    assert _post(server + "/api/config", None, raw=big)[0] == 413


def test_user_config_not_downloadable(server):
    # Even after the override file exists, it must not be GET-able (it's not a
    # secret, but the allowlist stays strict — only state the page reads).
    _post(server + "/api/config", {"tier": "auto", "overrides": {}})
    assert _status(server + "/user_config.json")[0] == 404


# ── POST /api/control (runtime control for an external bot) ───────────────────

def test_control_halt_and_status(server):
    status, body = _post(server + "/api/control", {"action": "halt", "reason": "test"})
    assert status == 200
    j = json.loads(body)
    assert j["ok"] is True and j["state"]["manual_halt"] is True
    assert os.path.exists("control_state.json")
    _, body2 = _post(server + "/api/control", {"action": "status"})
    assert json.loads(body2)["state"]["manual_halt"] is True


def test_control_pause_strategy(server):
    status, body = _post(server + "/api/control", {"action": "pause", "strategy": "iv_rank"})
    assert status == 200
    assert "iv_rank" in json.loads(body)["state"]["paused_strategies"]


def test_control_unknown_strategy_400(server):
    assert _post(server + "/api/control", {"action": "pause", "strategy": "bogus"})[0] == 400


def test_control_unknown_action_400(server):
    assert _post(server + "/api/control", {"action": "frobnicate"})[0] == 400


def test_control_flatten_requires_confirm(server):
    # No confirm → 400 BEFORE the kill switch is ever touched.
    code, body = _post(server + "/api/control", {"action": "flatten"})
    assert code == 400
    assert "confirm" in json.loads(body)["error"].lower()
