"""Regenerate the golden-store DDL from the field registry.

Run as ``python -m scripts.render_ddl``. The output is committed; a test
regenerates it and fails if the committed file has drifted from the registry.
"""

from __future__ import annotations

import pathlib
import sys

from cmdm.model.ddl import render_schema

REPO = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = REPO / "src" / "cmdm" / "sql" / "001_golden_schema.sql"


def main() -> int:
    sql = render_schema()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(sql, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(sql.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
