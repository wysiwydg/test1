#!/bin/sh
set -e
cd "$(dirname "$0")"
[ -f config.sh ] && . ./config.sh
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
: "${CMDM_HOST:=127.0.0.1}"; : "${CMDM_PORT:=8000}"
CMDM_DSN="$("$PY" -m cmdm.embedded dsn)"; export CMDM_DSN
"$PY" -m scripts.bootstrap --dsn "$CMDM_DSN"
echo; echo "Console:  http://$CMDM_HOST:$CMDM_PORT/console"; echo
exec "$PY" -m uvicorn cmdm.api.app:app --host "$CMDM_HOST" --port "$CMDM_PORT"
