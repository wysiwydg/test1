@echo off
setlocal
cd /d "%~dp0"
python update.py %*
endlocal
