@echo off
cd /d %~dp0
if not exist logs mkdir logs
REM start_watchdog.bat — BUG_0353 watchdog entry point (auto-restart / self-recovery).
REM
REM SUPERVISED launcher: runs start.bat (the single-run launcher, unchanged) in a
REM loop and restarts it ~5s after each exit, logging a timestamped [WATCHDOG] line
REM to logs\watchdog.log. Use start.bat for a one-shot launch; use this file when
REM you want the server to self-recover from an unhandled-exception crash.
setlocal
:loop
call start.bat
echo [%date% %time%] [WATCHDOG] server exited (code %errorlevel%) - restarting in 5s >> logs\watchdog.log
timeout /t 5 /nobreak >nul
goto loop
