@echo off
setlocal
cd /d "%~dp0"
if exist config.cmd call config.cmd
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
echo Installing Customer MDM from bundled wheels...
python install.py %*
