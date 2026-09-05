#!/bin/sh
# Install the converter from the wheels in this folder. No network needed.
exec python3 "$(dirname "$0")/install.py" "$@"
