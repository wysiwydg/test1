@echo off
setlocal
cd /d "%~dp0"
if exist config.cmd call config.cmd
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
if "%CMDM_HOST%"=="" set CMDM_HOST=127.0.0.1
if "%CMDM_PORT%"=="" set CMDM_PORT=8000
echo Starting the embedded PostgreSQL...
for /f "usebackq delims=" %%i in (`%PY% -m cmdm.embedded dsn`) do set "CMDM_DSN=%%i"
if "%CMDM_DSN%"=="" (echo Could not start the embedded PostgreSQL. & exit /b 1)
echo Applying migrations and registering console logins...
%PY% -m scripts.bootstrap --dsn "%CMDM_DSN%"
echo.
echo Console:  http://%CMDM_HOST%:%CMDM_PORT%/console
echo API docs: http://%CMDM_HOST%:%CMDM_PORT%/openapi.json
echo.
%PY% -m uvicorn cmdm.api.app:app --host %CMDM_HOST% --port %CMDM_PORT%
