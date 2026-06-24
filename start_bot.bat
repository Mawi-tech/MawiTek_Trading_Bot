@echo off
:: MawiTek Trading Bot — auto-restart launcher
:: Double-click this or add it to Task Scheduler.
:: The bot restarts automatically if it crashes.

title MawiTek Trading Bot

:loop
echo [%date% %time%] Starting bot...
python "A:\Mawitek Trading Bot\MawiTek_Trading_Bot\MawiTek_Trading_Bot\executor.py"
echo [%date% %time%] Bot exited (crash or stop). Restarting in 30 seconds...
timeout /t 30 /nobreak
goto loop
