"""
Cross-site request forgery defence for the dashboard's POST endpoints.

/api/control can halt the bot and flatten every position. Binding the server to
loopback does not protect it: loopback restricts which MACHINES can reach the
port, while CSRF is delivered by the operator's own browser, which is on the
same machine. Any page the operator visits could otherwise fire a CORS "simple
request" — text/plain body, no preflight — and this server would execute it.
The confirm=FLATTEN token is published in the README, so it is not a secret.

These tests pin the three checks that make that impossible, on both mutating
endpoints, and prove the handler is never reached when a check fires.
"""

import json
import http.client
import os
import threading

import pytest
from http.server import ThreadingHTTPServer

import dashboard_server as dsrv


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.DashboardRequestHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(port, path, payload, *, content_type="application/json",
          host=None, extra_headers=None):
    """
    Raw HTTP POST with full header control.

    urllib synthesises Host and refuses to let us omit headers cleanly, so the
    CSRF tests drive http.client directly — the headers ARE the test subject.
    An explicit Host in `headers` suppresses http.client's own, which is how we
    forge the DNS-rebinding case.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode()
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    if host is not None:
        headers["Host"] = host
    headers.update(extra_headers or {})
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.fixture()
def no_control_calls(monkeypatch):
    """Trip a flag if bot_control is ever reached. Nothing should touch it when
    a cross-site check rejects the request."""
    import bot_control
    called = []
    for name in ("halt", "resume", "flatten", "status"):
        monkeypatch.setattr(bot_control, name,
                            lambda *a, _n=name, **k: called.append(_n) or {})
    return called


# ── 1. Content type must be application/json ─────────────────────────────────

def test_text_plain_rejected_and_handler_not_reached(server, no_control_calls):
    """The exact CSRF payload shape: text/plain avoids a CORS preflight."""
    code, _ = _post(server, "/api/control",
                    {"action": "flatten", "confirm": "FLATTEN"},
                    content_type="text/plain")
    assert code == 415
    assert no_control_calls == []


def test_form_urlencoded_rejected(server, no_control_calls):
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    content_type="application/x-www-form-urlencoded")
    assert code == 415
    assert no_control_calls == []


def test_json_charset_parameter_accepted(server):
    """`application/json; charset=utf-8` is still application/json."""
    code, _ = _post(server, "/api/control", {"action": "status"},
                    content_type="application/json; charset=utf-8")
    assert code == 200


# ── 2. Cross-site provenance ─────────────────────────────────────────────────

def test_sec_fetch_site_cross_site_rejected(server, no_control_calls):
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    extra_headers={"Sec-Fetch-Site": "cross-site"})
    assert code == 403
    assert no_control_calls == []


def test_sec_fetch_site_same_site_rejected(server, no_control_calls):
    """same-site is a different origin (sibling subdomain) — still not us."""
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    extra_headers={"Sec-Fetch-Site": "same-site"})
    assert code == 403
    assert no_control_calls == []


def test_mismatched_origin_rejected(server, no_control_calls):
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    extra_headers={"Origin": "https://evil.example.com"})
    assert code == 403
    assert no_control_calls == []


def test_null_origin_rejected(server, no_control_calls):
    """A sandboxed iframe sends Origin: null — it matches no host, so it fails."""
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    extra_headers={"Origin": "null"})
    assert code == 403
    assert no_control_calls == []


def test_matching_origin_accepted(server):
    code, _ = _post(server, "/api/control", {"action": "status"},
                    extra_headers={"Origin": f"http://127.0.0.1:{server}"})
    assert code == 200


# ── 3. Host pinning (DNS rebinding) ──────────────────────────────────────────

def test_non_loopback_host_rejected(server, no_control_calls):
    """DNS rebinding: an attacker domain re-resolved to 127.0.0.1 reaches us
    with a genuinely same-origin browser context. The Host header still says
    who the browser thinks it is talking to."""
    code, _ = _post(server, "/api/control", {"action": "halt"},
                    host="evil.example.com")
    assert code == 421
    assert no_control_calls == []


@pytest.mark.parametrize("host", ["localhost:9", "127.0.0.1:9", "[::1]:9", "LOCALHOST:9"])
def test_loopback_hosts_accepted(server, host):
    code, _ = _post(server, "/api/control", {"action": "status"}, host=host)
    assert code == 200


def test_host_only_keeps_ipv6_brackets():
    """A naive rsplit(':') would shred `[::1]:8000` into `[`."""
    assert dsrv._host_only("[::1]:8000") == "[::1]"
    assert dsrv._host_only("localhost:8000") == "localhost"
    assert dsrv._host_only("LOCALHOST") == "localhost"


# ── Both endpoints share one implementation ──────────────────────────────────

@pytest.mark.parametrize("path", ["/api/config", "/api/control"])
def test_checks_apply_to_both_endpoints(server, path):
    assert _post(server, path, {}, content_type="text/plain")[0] == 415
    assert _post(server, path, {}, extra_headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _post(server, path, {}, host="evil.example.com")[0] == 421


# ── The legitimate path still works ──────────────────────────────────────────

def test_same_origin_json_reaches_handler(server):
    code, body = _post(server, "/api/control", {"action": "halt", "reason": "test"},
                       extra_headers={"Sec-Fetch-Site": "same-origin"})
    assert code == 200
    assert json.loads(body)["state"]["manual_halt"] is True
    assert os.path.exists("control_state.json")


def test_flatten_still_requires_confirm(server):
    """The confirm token is a typo guard, not a CSRF defence — but it must
    survive this patch."""
    code, body = _post(server, "/api/control", {"action": "flatten"},
                       extra_headers={"Sec-Fetch-Site": "same-origin"})
    assert code == 400
    assert "confirm" in json.loads(body)["error"].lower()


def test_config_post_still_saves(server):
    code, body = _post(server, "/api/config",
                       {"tier": "auto", "overrides": {"risk_per_trade_pct": 0.5}},
                       extra_headers={"Sec-Fetch-Site": "same-origin"})
    assert code == 200
    assert json.loads(body)["ok"] is True
