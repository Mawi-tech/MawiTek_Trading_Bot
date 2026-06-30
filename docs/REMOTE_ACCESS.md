# Monitor MawiTek from anywhere — remote dashboard + alerts

Two pieces: **see the dashboard on your phone** (Tailscale) and **get pinged when
something happens** (Discord / Email / SMS). Both are private and use no paid
services.

---

## 1. Remote dashboard over Tailscale (private)

Tailscale is a free, zero-config VPN. Your dashboard stays reachable only by your
own signed-in devices — it is **never exposed to the public internet**.

**One-time setup**
1. Install Tailscale on this PC and your phone: <https://tailscale.com/download>
2. Sign in to the **same account** on both. They now share a private "tailnet".

### Option A — direct bind (works without any Tailscale account toggle)

1. Start the tailnet dashboard:
   ```
   serve_tailscale.bat
   ```
   It binds the dashboard to this PC's Tailscale IP (only your tailnet can reach
   it — not the public internet, not your home LAN).
2. **One-time firewall rule** — Windows blocks inbound :8000 by default. Run
   PowerShell **as Administrator** once:
   ```powershell
   New-NetFirewallRule -DisplayName "MawiTek Dashboard" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000
   ```
   Safe: the server only listens on the Tailscale IP, so this does **not** expose
   the dashboard to your local network.
3. On your phone (Safari, Tailscale on), open:
   ```
   http://your-machine:8000/dashboard.html
   http://100.x.y.z:8000/dashboard.html      (if the name doesn't resolve)
   ```
   (`your-machine` = this PC's Tailscale machine name; full name
   `your-machine.your-tailnet.ts.net`.)

### Option B — Tailscale Serve (HTTPS, no firewall rule, needs one account click)

1. Enable Serve for your tailnet once (Tailscale gates it on your account): open
   the `https://login.tailscale.com/f/serve?...` URL it prints and approve.
2. Then: `tailscale serve --bg 8000` → access at
   `https://your-machine.your-tailnet.ts.net/dashboard.html`. Stop with
   `tailscale serve reset`.

### Recommended either way — require a login

Set a dashboard password in `.env` (HTTP Basic Auth, enforced when **both** are set):
```
DASH_AUTH_USER=you
DASH_AUTH_PASS=use-a-long-passphrase
```

> Prefer a public link instead? `start_tunnel.bat` (Cloudflare quick tunnel) still
> works, but **set `DASH_AUTH_USER`/`DASH_AUTH_PASS` first** — a public tunnel has
> no other protection.

---

## 2. Alerts & notifications

The bot pushes high-signal events (fills, position closes incl. scale-outs and
trailing stops, daily-loss halts, big ±20% moves, and fresh scanner setups) to
any channels you configure in `.env`. Configure one or more:

**Discord** — easiest rich alerts:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```
(Server Settings → Integrations → Webhooks → New Webhook → Copy URL.)

**Email**:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password          # Gmail: use an App Password, not your login
ALERT_EMAIL_FROM=you@gmail.com
ALERT_EMAIL_TO=you@gmail.com
```

**SMS** (text messages, via your carrier's free email-to-SMS gateway — reuses the
SMTP settings above, no extra account):
```
ALERT_SMS_TO=5551234567@vtext.com        # Verizon
# AT&T: @txt.att.net   T-Mobile: @tmomail.net   (search "<carrier> email to SMS gateway")
```
By default SMS only fires for **urgent** events (fills/closes/halts/big moves), not
the frequent scanner heads-ups. Set `SMS_ALL_EVENTS=true` to text everything.

**Test your setup** (sends a real message to each enabled channel):
```
python event_notifier.py            # send a test to every configured channel
python event_notifier.py --status   # just show what's configured (no send)
```
The dashboard's **Strategies** tab shows which channels are live (`Alerts: ● Discord …`).
