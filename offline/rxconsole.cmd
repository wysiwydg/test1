@echo off
REM The Reflex console. Serves the frontend compiled when this bundle was built
REM and runs the Python backend behind it -- no Node, no npm, no network.
REM
REM The server-rendered console at http://127.0.0.1:8000/console is unaffected
REM and still runs. This is a second UI beside it, not a replacement.
setlocal
set HERE=%~dp0
if exist "%HERE%config.cmd" call "%HERE%config.cmd"
if "%CMDM_RX_PORT%"=="" set CMDM_RX_PORT=8100

"%HERE%.venv\Scripts\python.exe" -c "import reflex" 2>NUL
if errorlevel 1 (
  echo This bundle was built without the Reflex console.
  echo The server-rendered console at /console is unaffected: start.cmd
  exit /b 1
)

cd /d "%HERE%rxapp"
"%HERE%.venv\Scripts\python.exe" -m reflex run --env prod --backend-port %CMDM_RX_PORT% --frontend-port %CMDM_RX_PORT%
