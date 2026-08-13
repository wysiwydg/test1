#!/bin/sh
set -e
cd "$(dirname "$0")"
[ -f config.sh ] && . ./config.sh
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
python3 install.py "$@"
