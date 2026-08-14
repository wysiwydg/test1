#!/usr/bin/env sh
# The Reflex console.
#
# Serves the frontend that was compiled when this bundle was built, and runs the
# Python backend behind it. No Node, no npm, no network -- the compile already
# happened on the build host, and what is here is the output.
#
# The server-rendered console at http://127.0.0.1:8000/console is unaffected and
# still runs. This is a second UI beside it, not a replacement.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/config.sh" ] && . "$HERE/config.sh"
export CMDM_DSN="${CMDM_DSN:-}"
PORT="${CMDM_RX_PORT:-8100}"

if ! "$HERE/.venv/bin/python" -c "import reflex" 2>/dev/null; then
  echo "This bundle was built without the Reflex console." >&2
  echo "The server-rendered console at /console is unaffected: ./start.sh" >&2
  exit 1
fi

cd "$HERE/rxapp"
exec "$HERE/.venv/bin/python" -m reflex run --env prod \
     --backend-port "$PORT" --frontend-port "$PORT"
