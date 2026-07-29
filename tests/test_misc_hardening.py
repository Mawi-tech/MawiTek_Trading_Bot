"""
Small hardening fixes that each have one sharp edge.

  * X-XSS-Protection: the legacy auditor's blocking mode is deprecated and has
    known bypasses that can introduce vulnerabilities. Explicitly off, CSP only.
  * hmac.compare_digest raises TypeError on non-ASCII str, so an accented
    passphrase turned a wrong password into a 500 instead of a clean denial —
    and the `and` short-circuit made a valid username timing-distinguishable.
  * os.replace is atomic but not durable; the directory entry needs its own
    fsync or a power cut can undo a rename the write already returned from.
  * A process killed between the write and the replace leaves a *.tmp orphan
    that no reader ever sees and nothing ever cleans up.
"""

import base64
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
from http.server import ThreadingHTTPServer

import dashboard_server as dsrv
import state_io


# ── X-XSS-Protection ─────────────────────────────────────────────────────────

@pytest.fixture()
def plain_server(tmp_path):
    (tmp_path / "dashboard.html").write_text("<html>ok</html>")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.DashboardRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.chdir(cwd)


def test_xss_auditor_is_explicitly_disabled(plain_server):
    with urllib.request.urlopen(plain_server + "/dashboard.html", timeout=5) as r:
        assert r.headers.get("X-XSS-Protection") == "0"
        # The header we actually rely on is still there.
        assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")


# ── Basic auth with a non-ASCII password ─────────────────────────────────────

@pytest.fixture()
def auth_server(tmp_path, monkeypatch):
    (tmp_path / "dashboard.html").write_text("<html>ok</html>")
    monkeypatch.setattr(dsrv, "_AUTH_ENABLED", True)
    monkeypatch.setattr(dsrv, "_AUTH_USER", "opérateur")
    monkeypatch.setattr(dsrv, "_AUTH_PASS", "pässwörd-ünïcode")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.DashboardRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.chdir(cwd)


def _get_with_creds(url, user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("user,pw", [
    ("opérateur", "wrong"),
    ("wrong", "pässwörd-ünïcode"),
    ("wrong", "wrong"),
    ("opérateur", "pässwörd-ünïcodé"),   # one accent off
])
def test_wrong_nonascii_credentials_return_401_not_500(auth_server, user, pw):
    """compare_digest(str) raises TypeError on non-ASCII — that surfaced as a
    500, which both leaks that the comparison happened and breaks the client's
    retry prompt."""
    assert _get_with_creds(auth_server + "/dashboard.html",user, pw) == 401


def test_correct_nonascii_credentials_are_accepted(auth_server):
    assert _get_with_creds(auth_server + "/dashboard.html","opérateur", "pässwörd-ünïcode") == 200


def test_both_comparisons_run_unconditionally(monkeypatch):
    """`and` would skip the password compare on a wrong username, making a
    VALID username measurably faster to probe for."""
    calls = []
    real = dsrv.hmac.compare_digest

    def _counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(dsrv, "_AUTH_ENABLED", True)
    monkeypatch.setattr(dsrv, "_AUTH_USER", "user")
    monkeypatch.setattr(dsrv, "_AUTH_PASS", "pass")
    monkeypatch.setattr(dsrv.hmac, "compare_digest", _counting)

    handler = dsrv.DashboardRequestHandler.__new__(dsrv.DashboardRequestHandler)
    token = base64.b64encode(b"totally-wrong:also-wrong").decode()
    handler.headers = {"Authorization": f"Basic {token}"}

    assert dsrv.DashboardRequestHandler._authorized(handler) is False
    assert len(calls) == 2, "password compare was short-circuited away"


# ── Durability of the atomic write ───────────────────────────────────────────

def test_write_still_lands_with_directory_fsync(tmp_path):
    """The fsync is best-effort (Windows has no directory fd) and must never
    turn a good write into a failure."""
    p = str(tmp_path / "durable.json")
    state_io.atomic_write_json(p, {"a": 1})
    assert state_io.read_json(p) == {"a": 1}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_fsync_dir_swallows_an_unopenable_path(tmp_path):
    state_io._fsync_dir(str(tmp_path / "does-not-exist"))   # must not raise


# ── Stale temp-file sweeper ──────────────────────────────────────────────────

def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_sweeper_removes_only_old_temp_files(tmp_path):
    old_tmp = tmp_path / "risk_state.json.1234.999.tmp"
    new_tmp = tmp_path / "risk_state.json.5678.111.tmp"
    old_tmp.write_text("{}")
    new_tmp.write_text("{}")
    _age(old_tmp, 7200)

    removed = state_io.sweep_stale_temp_files(str(tmp_path), older_than=3600)

    assert removed == [str(old_tmp)]
    assert not old_tmp.exists()
    assert new_tmp.exists(), "deleted a temp file a live writer may still own"


def test_sweeper_never_touches_live_state_files(tmp_path):
    """The age floor alone is not enough — an old, perfectly healthy
    risk_state.json is exactly what this must not eat."""
    keep = ["risk_state.json", "pending_orders.json", "closed_trades.json",
            ".env", "dashboard.html", "state_io.py", "risk_state.json.lock",
            "risk_state.json.corrupt.1700000000"]
    for name in keep:
        p = tmp_path / name
        p.write_text("live")
        _age(p, 999999)

    assert state_io.sweep_stale_temp_files(str(tmp_path), older_than=1) == []
    for name in keep:
        assert (tmp_path / name).exists()


def test_sweeper_ignores_directories_named_like_temp_files(tmp_path):
    d = tmp_path / "build.tmp"
    d.mkdir()
    _age(d, 999999)
    assert state_io.sweep_stale_temp_files(str(tmp_path), older_than=1) == []
    assert d.is_dir()


def test_sweeper_on_a_missing_directory_returns_empty(tmp_path):
    assert state_io.sweep_stale_temp_files(str(tmp_path / "nope")) == []


def test_sweeper_leaves_a_fresh_atomic_write_alone(tmp_path):
    """End to end: a write happening right now must survive a concurrent sweep."""
    p = str(tmp_path / "state.json")
    state_io.atomic_write_json(p, {"n": 1})
    assert state_io.sweep_stale_temp_files(str(tmp_path), older_than=0.0) == []
    assert state_io.read_json(p) == {"n": 1}
