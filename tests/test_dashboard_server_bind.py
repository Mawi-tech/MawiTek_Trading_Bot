"""
The dashboard refuses to listen beyond loopback without a password.

/api/control halts the bot and flattens every open position, and it is
unauthenticated whenever DASH_AUTH_USER/DASH_AUTH_PASS are unset — the default.
`--bind 0.0.0.0` on an untrusted network therefore hands a kill switch to
anyone who portscans the subnet. That combination must be impossible to start,
not merely warned about.
"""

import pytest

import dashboard_server as dsrv


class _FakeServer:
    """Stands in for the real ThreadingHTTPServer so main() can run to
    completion without ever binding a socket or blocking in serve_forever."""

    def __init__(self):
        self.served = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        pass


@pytest.fixture()
def run_main(tmp_path, monkeypatch):
    """Drive main() with the network and browser stubbed out. Returns a callable
    taking (bind, auth_enabled) and yielding the fake server it built."""
    (tmp_path / "dashboard.html").write_text("<html></html>")
    (tmp_path / "backtest_dashboard.html").write_text("<html></html>")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dsrv, "_open_browser_after_delay", lambda *a, **k: None)

    def _run(bind, auth_enabled=False):
        built = _FakeServer()
        monkeypatch.setattr(dsrv, "_AUTH_ENABLED", auth_enabled)
        monkeypatch.setattr(dsrv, "_build_server", lambda b, p: built)
        monkeypatch.setattr(
            "sys.argv",
            ["dashboard_server.py", "--no-browser", "--bind", bind, "--dir", str(tmp_path)],
        )
        dsrv.main()
        return built

    return _run


# ── The refusal ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.50", "100.64.0.1", "::", "10.0.0.5"])
def test_non_loopback_without_auth_exits_2(run_main, bind, capsys):
    with pytest.raises(SystemExit) as exc:
        run_main(bind, auth_enabled=False)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    # The message must name the two variables the operator has to set.
    assert "DASH_AUTH_USER" in err
    assert "DASH_AUTH_PASS" in err


def test_refusal_happens_before_binding(run_main, monkeypatch):
    """The exit must precede _build_server — otherwise the exposed socket is
    briefly live before we bail."""
    def _boom(bind, port):
        raise AssertionError("_build_server must not run when auth is missing")
    monkeypatch.setattr(dsrv, "_build_server", _boom)
    with pytest.raises(SystemExit):
        run_main("0.0.0.0", auth_enabled=False)


# ── The permitted cases ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.50", "::"])
def test_non_loopback_with_auth_proceeds(run_main, bind):
    assert run_main(bind, auth_enabled=True).served is True


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_without_auth_proceeds(run_main, bind):
    """The default, and the whole point of the default: no password needed when
    nothing off-machine can reach the port."""
    assert run_main(bind, auth_enabled=False).served is True


# ── The predicate itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_is_loopback_true(host):
    assert dsrv._is_loopback_bind(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "100.64.0.1", "", "example.com"])
def test_is_loopback_false(host):
    """Note "" and 0.0.0.0 mean EVERY interface, and an unresolvable name is
    treated as exposed — the failure direction must cost a config error, never
    an open kill switch."""
    assert dsrv._is_loopback_bind(host) is False
