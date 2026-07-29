# Control API

A small HTTP surface for steering the **running** trading bot from an **external**
process — your separate Discord bot, a script, or `curl`. The trading bot and your
Discord bot stay completely separate; the bot just calls these endpoints.

Served by `dashboard_server.py` (the same server that serves the dashboard).
Loopback-only by default; see **Auth** below before exposing it.

## Read state

- `GET /dashboard_state.json` — full snapshot the bot writes every scan cycle
  (equity, P&L, positions, setups, tier config, alert prefs, channel status).
- `POST /api/control {"action": "status"}` — just the control state (halt/paused).

## Control commands — `POST /api/control`

Body is JSON `{"action": ...}`. Responses are JSON: `{"ok": true, "action": ...,
"state": {...}}` on success, `{"ok": false, "error": "..."}` (HTTP 400/500) on
failure.

| action            | body                                      | effect |
|-------------------|-------------------------------------------|--------|
| `status`          | —                                         | return control state |
| `halt`            | `{"reason": "..."}` (optional)            | block **all** new entries (manual halt) |
| `resume`          | —                                         | clear the manual halt |
| `pause`           | `{"strategy": "hft_intraday"}`            | block new entries for one strategy |
| `unpause`         | `{"strategy": "hft_intraday"}`            | unpause one strategy |
| `flatten`         | `{"confirm": "FLATTEN", "reason": "..."}` | **EMERGENCY**: cancel orders + market-close everything, then halt |

Strategy names: `catalyst_long_call`, `iv_rank`, `hft_intraday`, `pead`, `bounce`.

`control_state` shape: `{"manual_halt": bool, "halt_reason": str,
"paused_strategies": [str], "updated_at": str}`.

A manual halt and per-strategy pauses are enforced in `pre_trade_check`, so they
take effect on the next scan cycle — no restart. The manual halt is **separate**
from the daily-loss auto-halt: `resume` only clears the manual one (it can never
un-halt a real daily-loss breach).

### Examples

```bash
# Stop opening new trades
curl -X POST localhost:8000/api/control -H 'Content-Type: application/json' \
     -d '{"action":"halt","reason":"news risk"}'

# Pause just the day-trading strategy
curl -X POST localhost:8000/api/control -H 'Content-Type: application/json' \
     -d '{"action":"pause","strategy":"hft_intraday"}'

# Emergency flatten (closes positions — requires the confirm token)
curl -X POST localhost:8000/api/control -H 'Content-Type: application/json' \
     -d '{"action":"flatten","confirm":"FLATTEN"}'
```

From a Discord bot (pseudocode), on a slash command:

```js
const r = await fetch("http://localhost:8000/api/control", {
  method: "POST",
  headers: { "Content-Type": "application/json", ...authHeader },
  body: JSON.stringify({ action: "halt", reason: `by ${user}` }),
});
const { ok, state } = await r.json();
```

## Auth

- **Default:** the server binds to `127.0.0.1` (loopback) — only processes on the
  same machine can reach it. If your Discord bot runs on the same box, nothing
  more is needed.
- **Remote / exposed:** set `DASH_AUTH_USER` and `DASH_AUTH_PASS` in `.env` to
  require HTTP Basic auth on every request (GET and POST), and only then bind
  beyond loopback (`python dashboard_server.py --bind 0.0.0.0`). Put it behind a
  tunnel/VPN (e.g. Tailscale) rather than the open internet.
- `flatten` additionally requires `"confirm": "FLATTEN"` so it can't fire by
  accident. This is a typo guard, not a security control — the token is
  published right here.

## Cross-site protection

Every POST is screened before its body is read. A caller that fails any check
never reaches `bot_control`:

| Requirement | Failure |
|---|---|
| `Content-Type: application/json` (a `; charset=…` parameter is fine) | `415` |
| `Sec-Fetch-Site`, if the client sends it, is `same-origin` or `none` | `403` |
| `Origin`, if present, has the same host as `Host` | `403` |
| `Host` is `localhost`, `127.0.0.1`, `::1`, or `[::1]` (port ignored) | `421` |

The content-type rule is what actually stops CSRF: `application/json` is not a
CORS-safelisted content type, so a cross-origin `fetch()` carrying it must pass
a preflight, and this server answers none. The `Host` rule stops DNS rebinding,
where an attacker-controlled name re-resolves to `127.0.0.1` and thereby gets a
same-origin browser context.

Command-line clients (curl, your Discord bot) send no `Sec-Fetch-Site` or
`Origin`, so they are unaffected — set the content type and use a loopback host.

## CLI alternative

Every command also runs locally without HTTP:

```
python bot_control.py status
python bot_control.py halt "reason"
python bot_control.py resume
python bot_control.py pause hft_intraday
python bot_control.py unpause hft_intraday
python bot_control.py flatten --confirm
```
