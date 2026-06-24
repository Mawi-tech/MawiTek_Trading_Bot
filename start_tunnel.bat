@echo off
:: Exposes the local dashboard to the internet via Cloudflare Tunnel (free).
:: Requires cloudflared.exe — download from:
::   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
::
:: Place cloudflared.exe somewhere on your PATH or in this folder, then run this.
:: It will print a https://*.trycloudflare.com URL — open that on your phone.

title MawiTek Tunnel

:loop
echo [%date% %time%] Starting Cloudflare tunnel...
cloudflared tunnel --url http://localhost:8000
echo [%date% %time%] Tunnel dropped. Restarting in 15 seconds...
timeout /t 15 /nobreak
goto loop
