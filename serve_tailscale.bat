@echo off
:: ===========================================================================
::  MawiTek dashboard over Tailscale SERVE — private HTTPS on your tailnet.
:: ===========================================================================
::  Serve is already configured and PERSISTS across reboots, so you normally
::  don't need to run this at all — just keep start_all running (it serves the
::  dashboard on localhost:8000, which Serve proxies over HTTPS).
::
::  Use this script only to re-assert the Serve config or print your URL.
::
::  Your private dashboard:
::      https://your-machine.your-tailnet.ts.net/dashboard.html
::  (open on any device signed into your tailnet, e.g. your iPhone)
::
::  Recommended: require a login — set in .env:
::      DASH_AUTH_USER=you
::      DASH_AUTH_PASS=use-a-long-passphrase
::
::  Stop sharing:  tailscale serve reset
:: ===========================================================================

title MawiTek Dashboard (Tailscale Serve)

set "TS=tailscale"
where tailscale >nul 2>&1 || set "TS=A:\Mawitek Trading Bot\tailscale.exe"

echo Ensuring the dashboard is served over HTTPS on your tailnet ...
"%TS%" serve --bg 8000
echo.
echo === serve status ===
"%TS%" serve status
echo.
echo Open on your tailnet devices:
echo     https://your-machine.your-tailnet.ts.net/dashboard.html
pause
