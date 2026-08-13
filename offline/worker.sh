#!/bin/sh
set -e
cd "$(dirname "$0")"
[ -f config.sh ] && . ./config.sh
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
CMDM_DSN="$("$PY" -m cmdm.embedded dsn)"; export CMDM_DSN
exec "$PY" -m cmdm.worker "$@"
