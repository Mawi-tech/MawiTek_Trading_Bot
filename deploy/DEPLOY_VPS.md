# Deploy — MawiTek Trading Bot on a VPS

Runs the full trading fleet 24/7 under `systemd`. The dashboard/control API is
reachable only over Tailscale (with auth). Setup alerts post to Discord via the
webhook (works over the public internet, no inbound ports needed).

> **⚠️ Real-money cutover rule:** only ONE trading fleet may run against your
> Tradier account at a time. Before starting the VPS fleet, **fully stop the PC
> fleet and remove its autostart** (`MawiTek Trading Bot.lnk` in the PC Startup
> folder). Confirm zero trading processes on the PC. Two fleets on two machines
> would double-execute — the loopback single-instance guard does NOT cross hosts.

## 0. Provision
- **Ubuntu 24.04 LTS**, **2 GB RAM minimum (4 GB comfortable)** — pandas/numpy + 7
  processes. E.g. Hetzner CX22 (~€4.5/mo, 4 GB) or Oracle Cloud free tier.

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
> The bot targets 3.11+.

## 2. Tailscale (same tailnet as the Pi)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4        # note this VPS's 100.x address — the Pi's TRADING_API_URL
# allow the dashboard/control port ONLY from the tailnet
sudo ufw allow in on tailscale0 to any port 8000 proto tcp
```

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
- **`DASH_AUTH_USER` / `DASH_AUTH_PASS`** ← the control API now requires auth;
  these must match the Pi's `TRADING_API_USER` / `TRADING_API_PASS`
- `CONTACT_EMAIL` (used in the social/reddit User-Agent)
```bash
chmod 600 .env
```

### Bind the dashboard beyond loopback (Tailscale-only)
The dashboard/control server must listen on all interfaces so Tailscale can reach
it — `ufw` above restricts it to the tailnet, and auth is required on every call.
`start_all.py` launches `dashboard_server.py --no-browser`; add the bind flag:
```bash
# edit start_all.py COMPONENTS["dashboard"]["args"] to:  ["--no-browser", "--bind", "0.0.0.0"]
```
(Or run the dashboard as its own service with `--bind 0.0.0.0`.) Never expose port
8000 to the public internet — keep it tailnet-only.
```bash
exit   # back to your sudo user
```

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
# from the Pi (or any tailnet device), with auth:
curl -u USER:PASS http://<VPS-TAILSCALE-IP>:8000/api/control \
     -X POST -H 'Content-Type: application/json' -d '{"action":"status"}'
```
Then from Discord: `/trading status` should return live numbers.

## 7. Decommission the PC (do this LAST, once the VPS is confirmed healthy)
On the Windows PC:
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
