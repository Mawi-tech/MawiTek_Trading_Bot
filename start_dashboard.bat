@echo off
:: MawiTek Dashboard Server — auto-restart launcher

title MawiTek Dashboard

:loop
echo [%date% %time%] Starting dashboard server...
cd /d "A:\Mawitek Trading Bot\MawiTek_Trading_Bot\MawiTek_Trading_Bot" && python -m mawitek.dashboard.dashboard_server --port 8000 --no-browser
echo [%date% %time%] Dashboard exited. Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
