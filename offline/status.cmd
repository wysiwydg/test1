@echo off
setlocal
cd /d "%~dp0"
if exist config.cmd call config.cmd
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
%PY% -m cmdm.embedded status
echo.
if exist pgdata\server.log (
  echo --- last lines of pgdata\server.log ---
  powershell -NoProfile -Command "Get-Content pgdata\server.log -Tail 20"
) else (
  echo No pgdata\server.log yet - the server has not been started.
)
