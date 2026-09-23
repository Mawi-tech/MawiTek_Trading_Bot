# Deploy — MawiTek Trading Bot on a VPS

Runs the full trading fleet 24/7 under `systemd`. The dashboard/control API is
reachable only over Tailscale (with auth). Setup alerts post to Discord via the
webhook (works over the public internet, no inbound ports needed).

> **⚠️ Real-money cutover rule:** only ONE trading fleet may run against your
> Tradier account at a time. Before starting the VPS fleet, **fully stop the PC
> fleet and remove its autostart** (`MawiTek Trading Bot.lnk` in the PC Startup
> folder). Confirm zero trading processes on the PC. Two fleets on two machines
> would double-execute — the loopback single-instance guard does NOT cross hosts.
>
> **Cut over flat, and outside market hours.** "No processes running" is a weaker
> guarantee than "nothing to manage." If positions are open while both machines
> could believe they own the book, a mistake costs money rather than a restart.
> Close or hand off positions first, cut over on a weekend or after the close,
> and let the VPS fleet reconcile against the broker on its first run.

## 0. Provision
- **Ubuntu 24.04 LTS**, **4 GB RAM** (2 GB is the floor and it is tight — seven
  Python processes each with pandas resident run ~150 MB apiece before caches).
- **40–80 GB disk.** Not because the state is large, but because `logs/` grows
  roughly **1 GB/month unrotated** — see §8, which bounds it.
- **Put it in US-East.** This is the one thing that genuinely differs from the
  Discord bot's hosting. Tradier is a US broker, and `hft_executor` and
  `vwap_fade_executor` chase quotes; a European host adds ~90 ms to every order
  and quote round-trip, which is a fill-quality and quote-staleness problem, not
  just a latency number. Any US-East datacenter is within a few ms of a REST
  broker API — this is not colocation, so there is no reason to pay a premium
  for one specific facility.
- **Reasonable picks:** Vultr High Performance (New Jersey or Atlanta) or
  DigitalOcean NYC, 2 vCPU / 4 GB, about $24/mo. Prefer NVMe over the cheaper
  SATA tiers; the state files are rewritten whole and often.
- **Do not use a free tier for this.** Oracle Cloud's always-free instances are
  reclaimed when idle and carry no SLA. An instance disappearing mid-session
  with open options positions is not a risk worth $24/mo.

## 1. Base setup + hardening
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git ufw
sudo useradd -m -s /bin/bash mawitek
sudo systemctl enable --now unattended-upgrades || sudo apt -y install unattended-upgrades
# firewall: allow SSH, deny the rest inbound (dashboard is Tailscale-only, below)
sudo ufw allow OpenSSH
sudo ufw --force enable
```
> If your Python is older than 3.11, install a newer one (deadsnakes PPA or pyenv).
> The bot targets 3.11+. Ubuntu 24.04 ships 3.12, so this is normally a no-op.

## 2. Tailscale (same tailnet as the Discord bot's host)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4        # note this VPS's 100.x address — the Discord bot's TRADING_API_URL
# allow the dashboard/control port ONLY from the tailnet
sudo ufw allow in on tailscale0 to any port 8000 proto tcp
```

**Do this before §5.** The systemd unit binds the control API to `0.0.0.0` so
Tailscale can reach it. That is only safe because the rule above is already in
place; without it, port 8000 is reachable from the public internet behind
nothing but basic auth. §6 verifies this rather than assuming it.

## 3. Code + dependencies
```bash
sudo -iu mawitek
git clone <your-repo-url> ~/MawiTek_Trading_Bot     # or scp the folder up
cd ~/MawiTek_Trading_Bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 4. Configure `.env` (recreate by hand)
```bash
cp .env.example .env      # if present; otherwise create it
nano .env
```
Set at least:
- `TRADIER_API_KEY`, `TRADIER_ACCOUNT_ID` (real broker creds)
- `DISCORD_WEBHOOK_URL` (the #trading-signals webhook), `DISCORD_EVENT_KINDS=setup`
- **`DASH_AUTH_USER` / `DASH_AUTH_PASS`** ← the control API requires auth; these
  must match the Discord bot's `TRADING_API_USER` / `TRADING_API_PASS`
- `CONTACT_EMAIL` (used in the social/reddit User-Agent)
```bash
chmod 600 .env
exit   # back to your sudo user
```

> **`DASH_BIND` does not go in `.env`.** `start_all.py` deliberately does not
> load that file — it only text-scans it for a credentials pre-flight, to stay
> independent of the bot's imports. The bind address is a real process variable,
> set by `Environment=DASH_BIND=0.0.0.0` in the unit installed in §5. Earlier
> versions of this document told you to hand-edit `COMPONENTS` in
> `start_all.py`; don't — that edit is lost on the next `git pull`, and the
> symptom is a control API that silently stops answering.

## 5. Install the systemd service
```bash
sudo cp /home/mawitek/MawiTek_Trading_Bot/deploy/mawitek-trading.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mawitek-trading
```

## 6. Verify
```bash
systemctl status mawitek-trading
journalctl -u mawitek-trading -f          # watch the launcher + child startup
# from the Discord bot's host (or any tailnet device), with auth:
curl -u USER:PASS http://<VPS-TAILSCALE-IP>:8000/api/control \
     -X POST -H 'Content-Type: application/json' -d '{"action":"status"}'
```

**Then confirm the control API is NOT reachable from the public internet.** Run
this from somewhere that is not on the tailnet — a phone on cellular is fine:

```bash
curl --max-time 8 http://<VPS-PUBLIC-IP>:8000/     # MUST time out or be refused
```

If that returns anything at all, stop the fleet and fix §2 before going further.
A reply here means your broker credentials sit behind one basic-auth prompt on
the open internet.

Then from Discord: `/trading status` should return live numbers.

## 7. Back up the state
The JSON state in the project root *is* the trade record — `closed_trades.json`,
`equity_curve.json`, `pnl_history.json`, `decision_log.jsonl`, the `*_positions`
books — and it is what `/performance` reads. A VPS disk is not a backup, and a
provider snapshot lives in the same account as the thing it protects.

Back up the record, not the caches. `iv_history.json`, `liquidity_cache.json`,
`earnings_cache.json`, `sec_cik_map.json`, `news_feed.json` and
`social_sentiment.json` all regenerate, and they are most of the bytes.

```bash
sudo apt -y install restic
sudo -iu mawitek
# one-time, against a bucket on Backblaze B2 or Cloudflare R2:
restic init -r s3:<endpoint>/<bucket>/mawitek-trading
```

Then a script the timer calls:

```bash
#!/usr/bin/env bash
# ~/backup-state.sh
set -euo pipefail
cd /home/mawitek/MawiTek_Trading_Bot
restic backup \
  closed_trades.json equity_curve.json pnl_history.json decision_log.jsonl \
  ./*_positions.json \
  risk_state.json drawdown_state.json control_state.json halt_events.json events.json
restic forget --keep-daily 14 --keep-weekly 8 --prune
```

Put `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` and the S3 credentials in a
root-owned env file (`chmod 600`), run the script from a systemd timer or cron
around 02:00 ET, and **test a restore into a scratch directory** — a backup you
have never restored is a hypothesis, not a backup. Run `restic check` weekly.

`.env` is deliberately not in that list: it is secrets, and it belongs in a
password manager rather than an automated archive.

## 8. Keep the logs bounded
Two different things write logs here, and only one of them rotates itself.

`logger.py` uses a `RotatingFileHandler` capped at 10 MB, so the bot's own
structured logs are fine. But `logs/<component>.log` is each child process's
**stdout**, opened once by `start_all.py` and held open for the life of that
process. Nothing caps those. On the PC they reached **4.23 GB**, of which
`news_monitor.log` alone was **3.84 GB** over about four months.

Because the descriptor stays open, rotation here must use `copytruncate` —
renaming the file would leave the child writing to an unlinked inode and the
replacement would stay empty forever.

```bash
sudo tee /etc/logrotate.d/mawitek-trading >/dev/null <<'CONF'
/home/mawitek/MawiTek_Trading_Bot/logs/*.log {
    daily
    rotate 7
    size 50M
    copytruncate
    compress
    delaycompress
    missingok
    notifempty
    su mawitek mawitek
}
CONF
sudo logrotate --debug /etc/logrotate.d/mawitek-trading   # dry run, changes nothing
```

> Worth chasing separately: 3.84 GB from one component in four months is not
> normal volume, it is something logging per-item at debug level in a hot loop.
> Rotation stops it filling the disk; it does not make it correct.

## 9. Decommission the PC (do this LAST, once the VPS is confirmed healthy)

Copy the state up **before** the VPS fleet's first run, or it starts with an
empty book and no history: the `*_positions.json` books, `closed_trades.json`,
`equity_curve.json`, `pnl_history.json` and `decision_log.jsonl`. Leave the
caches behind; they rebuild on their own.

Then, on the Windows PC:

```powershell
# stop the fleet
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*start_all.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*MawiTek_Trading_Bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# remove its autostart so it never comes back
Remove-Item "$([Environment]::GetFolderPath('Startup'))\MawiTek Trading Bot.lnk"
```

Confirm zero trading processes on the PC before/while the VPS runs.

## Manage
```bash
sudo systemctl restart mawitek-trading    # clean stop (SIGINT → children) then start
sudo systemctl stop mawitek-trading
journalctl -u mawitek-trading -n 200
```

## Notes
- **Timezone:** the bot computes Eastern time internally (`utils.now_est`), so the
  VPS host timezone doesn't affect trading logic. Set the host to UTC for clean logs.
- **Single-instance guard** (loopback lock :8765) still protects against a double
  launch *on the VPS*; the cross-host rule in the banner is what protects the broker
  account across machines.
- **Moving the fleet off the desktop also takes its traffic off your home
  connection.** The scanners fan out hundreds of short-lived HTTP requests in
  bursts, which is enough to fill a home router's queue and add latency to
  everything else sharing that link — online games most noticeably.
