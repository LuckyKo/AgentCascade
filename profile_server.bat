@echo off
echo.
echo ============================================
echo   AgentCascade Profiler (py-spy)
echo ============================================
echo.

REM --- Step 1: Find the Python PID ---
echo [1/3] Finding Python server process...
for /f "tokens=2 delims=," %%a in ('tasklist /fi "imagename eq python.exe" /fo csv ^| findstr /i "python.exe"') do (
    set "PID=%%~a"
)

if "%PID%"=="" (
    echo ERROR: No python.exe process found. Is the server running?
    pause
    exit /b 1
)

echo   Found python.exe with PID: %PID%
echo.

REM --- Step 2: Ask how long to profile ---
set /p MINUTES="Minutes to profile (default 5): "
if "%MINUTES%"=="" set MINUTES=5
set /a SECONDS=%MINUTES%*60

echo   Profiling for %SECONDS% seconds...
echo.

REM --- Step 3: Run py-spy and generate flame graph ---
echo [3/3] Starting py-spy recording...
set "TIMESTAMP=%DATE:~-4%%DATE:~3,2%%DATE:~0,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "OUTPUT=profile_%TIMESTAMP%.svg"

py-spy record -d %SECONDS% -o "%OUTPUT%" --pid %PID%

echo.
if exist "%OUTPUT%" (
    echo Done! Profile saved to: %OUTPUT%
    echo Opening in browser...
    start "" "%OUTPUT%"
) else (
    echo ERROR: Profile file was not created. Check py-spy output above.
)
echo.
