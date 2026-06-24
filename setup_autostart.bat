@echo off
:: Run this ONCE as Administrator to register both tasks in Windows Task Scheduler.
:: After this, the bot and dashboard start automatically on boot.

echo Registering MawiTek bot autostart tasks...

:: Bot task
schtasks /create /tn "MawiTek Bot" ^
  /tr "\"A:\Mawitek Trading Bot\MawiTek_Trading_Bot\MawiTek_Trading_Bot\start_bot.bat\"" ^
  /sc ONSTART /delay 0001:00 /ru SYSTEM /f

:: Dashboard task
schtasks /create /tn "MawiTek Dashboard" ^
  /tr "\"A:\Mawitek Trading Bot\MawiTek_Trading_Bot\MawiTek_Trading_Bot\start_dashboard.bat\"" ^
  /sc ONSTART /delay 0001:30 /ru SYSTEM /f

echo.
echo Done. Tasks registered:
echo   - "MawiTek Bot"       starts 1 min after boot
echo   - "MawiTek Dashboard" starts 1.5 min after boot
echo.
echo To verify: Task Scheduler ^> Task Scheduler Library
echo To remove: schtasks /delete /tn "MawiTek Bot" /f
pause
