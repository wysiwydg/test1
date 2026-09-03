#!/bin/sh
# Check the converter against sas7bdat files generated on this machine.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m pytest tests -q
