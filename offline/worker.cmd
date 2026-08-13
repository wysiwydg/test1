@echo off
setlocal
cd /d "%~dp0"
if exist config.cmd call config.cmd
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
for /f "usebackq delims=" %%i in (`%PY% -m cmdm.embedded dsn`) do set "CMDM_DSN=%%i"
%PY% -m cmdm.worker %*
