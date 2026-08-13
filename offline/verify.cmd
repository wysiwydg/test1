@echo off
setlocal
cd /d "%~dp0"
if exist config.cmd call config.cmd
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
%PY% verify.py %*
if errorlevel 1 exit /b 1
