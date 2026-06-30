"""The log redactor must scrub any known secret value from log output, so a
stray exception or future edit can never leak a token/key/webhook to the logs.
"""

import logging

import logger as lg


def _rec(msg, args=None):
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, None)


def test_redacts_secret_in_message(monkeypatch):
    monkeypatch.setenv("TRADIER_API_KEY", "SeCrEtToken_abcdef123456")
    r = _rec("auth failed for https://api/bot SeCrEtToken_abcdef123456/x")
    assert lg._REDACTOR.filter(r) is True
    out = r.getMessage()
    assert "SeCrEtToken_abcdef123456" not in out
    assert "***REDACTED***" in out


def test_redacts_secret_passed_as_arg(monkeypatch):
    # logging's lazy "%s" formatting must still be scrubbed.
    monkeypatch.setenv("DISCORD_WEBHOOK_URL",
                       "https://discord.com/api/webhooks/42/SECRETPART9999")
    r = _rec("discord error: %s",
             ("posting to https://discord.com/api/webhooks/42/SECRETPART9999",))
    lg._REDACTOR.filter(r)
    assert "SECRETPART9999" not in r.getMessage()


def test_noop_without_secrets(monkeypatch):
    for k in lg._SECRET_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    r = _rec("ordinary log line, nothing secret here")
    assert lg._REDACTOR.filter(r) is True
    assert r.getMessage() == "ordinary log line, nothing secret here"


def test_short_values_not_redacted(monkeypatch):
    # A 6+ char floor avoids redacting trivial strings that could be substrings.
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "VA1234567")   # >=6 → redacted
    monkeypatch.setenv("DASH_AUTH_PASS", "abc")             # <6 → ignored
    r = _rec("acct VA1234567 pass abc")
    lg._REDACTOR.filter(r)
    out = r.getMessage()
    assert "VA1234567" not in out
    assert "abc" in out          # short value left alone
