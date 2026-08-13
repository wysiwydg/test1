#!/bin/sh
set -e
cd "$(dirname "$0")"
[ -f config.sh ] && . ./config.sh
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
"$PY" -m cmdm.embedded status
echo
if [ -f pgdata/server.log ]; then
  echo "--- last lines of pgdata/server.log ---"
  tail -20 pgdata/server.log
else
  echo "No pgdata/server.log yet - the server has not been started."
fi
