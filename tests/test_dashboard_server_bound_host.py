"""
A specific-address bind accepts its own IP as Host on the POST endpoints.

The Discord bot moves to a VPS and reaches /api/control over Tailscale, so its
requests carry `Host: 100.x.y.z:8000`. The DNS-rebinding check used to accept
loopback names only, which answered every remote halt/pause/flatten with 421.
These tests pin the widening to exactly the bound IP, and nothing else.
"""

import json
import http.client
import threading

import pytest
from http.server import ThreadingHTTPServer

import dashboard_server as dsrv

TS_IP = "100.101.102.103"


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


def _post_status(port, host):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps({"action": "status"}).encode()
    headers = {"Content-Type": "application/json",
               "Content-Length": str(len(body)), "Host": host}
    try:
        conn.request("POST", "/api/control", body=body, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


def test_bound_ip_accepted(server, monkeypatch):
    monkeypatch.setattr(dsrv, "_BOUND_HOST", TS_IP)
    assert _post_status(server, f"{TS_IP}:8000") == 200


def test_bound_ip_rejected_when_not_bound(server, monkeypatch):
    """The default (loopback bind) still refuses a remote-looking Host."""
    monkeypatch.setattr(dsrv, "_BOUND_HOST", None)
    assert _post_status(server, f"{TS_IP}:8000") == 421


def test_other_ip_rejected_when_bound(server, monkeypatch):
    monkeypatch.setattr(dsrv, "_BOUND_HOST", TS_IP)
    assert _post_status(server, "100.64.0.9:8000") == 421


def test_rebound_domain_rejected_when_bound(server, monkeypatch):
    """A domain rebound to the bound IP still arrives with the domain as Host."""
    monkeypatch.setattr(dsrv, "_BOUND_HOST", TS_IP)
    assert _post_status(server, "evil.example.com:8000") == 421


def test_loopback_still_accepted_when_bound(server, monkeypatch):
    monkeypatch.setattr(dsrv, "_BOUND_HOST", TS_IP)
    assert _post_status(server, "localhost:8000") == 200


@pytest.mark.parametrize("bind, expected", [
    (TS_IP, TS_IP),
    (f" {TS_IP} ", TS_IP),
    ("fd7a:115c:a1e0::1", "[fd7a:115c:a1e0::1]"),
    ("[fd7a:115c:a1e0:0::1]", "[fd7a:115c:a1e0::1]"),
    ("0.0.0.0", None),
    ("::", None),
    ("", None),
    ("127.0.0.1", None),
    ("::1", None),
    ("localhost", None),
    ("mypc.tailnet.ts.net", None),
])
def test_bound_host_alias(bind, expected):
    assert dsrv._bound_host_alias(bind) == expected


def test_alias_matches_host_only_format():
    """The alias must compare equal to what _host_only() extracts from Host."""
    assert dsrv._host_only(f"{TS_IP}:8000") == dsrv._bound_host_alias(TS_IP)
    assert (dsrv._host_only("[fd7a:115c:a1e0::1]:8000")
            == dsrv._bound_host_alias("fd7a:115c:a1e0::1"))


# ── DASH_ALLOWED_HOSTS: names a proxy (Tailscale Serve) forwards as Host ──────

SERVE_NAME = "mybox.tailnet.ts.net"


def test_listed_proxy_name_accepted(server, monkeypatch):
    """Tailscale Serve keeps the caller's Host — verified against a live Serve."""
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset({SERVE_NAME}))
    assert _post_status(server, SERVE_NAME) == 200


def test_unlisted_proxy_name_rejected(server, monkeypatch):
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset())
    assert _post_status(server, SERVE_NAME) == 421


def test_listed_name_does_not_admit_lookalikes(server, monkeypatch):
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset({SERVE_NAME}))
    assert _post_status(server, f"{SERVE_NAME}.evil.example.com") == 421


def test_proxied_hosts_require_auth(monkeypatch):
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset({SERVE_NAME}))
    monkeypatch.setattr(dsrv, "_AUTH_ENABLED", False)
    with pytest.raises(SystemExit):
        dsrv._require_auth_for_proxied_hosts()


def test_proxied_hosts_with_auth_start(monkeypatch):
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset({SERVE_NAME}))
    monkeypatch.setattr(dsrv, "_AUTH_ENABLED", True)
    dsrv._require_auth_for_proxied_hosts()


def test_no_proxied_hosts_needs_no_auth(monkeypatch):
    monkeypatch.setattr(dsrv, "_EXTRA_HOSTS", frozenset())
    monkeypatch.setattr(dsrv, "_AUTH_ENABLED", False)
    dsrv._require_auth_for_proxied_hosts()
