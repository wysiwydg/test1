#!/bin/sh
# Convert zipped sas7bdat extracts to CSV. Arguments pass straight through.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m scripts.sas2csv "$@"
