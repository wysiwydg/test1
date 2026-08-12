"""Bring an empty database up to a state the consoles can be used from.

Applies the migrations and registers one principal per console audience, then
prints each API key exactly once. There is no other way to get a key: secrets
are stored as a hash and a four-character prefix, so a key that is not written
down here has to be rotated rather than recovered.

    python -m scripts.bootstrap

The default set mirrors the three consoles rather than the five roles, because
"who needs a login" is answered by job, not by permission table:

    ops       INGESTOR + OPERATOR   submits files, watches them land
    steward   STEWARD               reviews ambiguous pairs, approves rules
    business  VIEWER                searches the customer book, PII masked

An existing subject is left alone and reported, so re-running this is safe and
does not silently invalidate keys that are already in use.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import psycopg

DEFAULT_PRINCIPALS: dict[str, tuple[str, ...]] = {
    "ops": ("INGESTOR", "OPERATOR"),
    "steward": ("STEWARD",),
    "business": ("VIEWER",),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.bootstrap",
        description="Apply migrations and register console principals.",
    )
    parser.add_argument(
        "--dsn", default=os.environ.get("CMDM_DSN"),
        help="PostgreSQL DSN; defaults to $CMDM_DSN",
    )
    parser.add_argument(
        "--principal", action="append", metavar="SUBJECT:ROLE[,ROLE]",
        help="register this principal instead of the defaults; repeatable",
    )
    parser.add_argument(
        "--skip-migrations", action="store_true",
        help="assume the schema is already present",
    )
    args = parser.parse_args(argv)

    if not args.dsn:
        parser.error("no DSN: pass --dsn or set CMDM_DSN")

    # Checked here rather than left to surface as a traceback from three frames
    # down. An API key is stored as a keyed digest, so registering a principal
    # needs the deployment secret, and this is the first command anyone runs.
    if not os.environ.get("CMDM_ID_HASH_KEY"):
        parser.error(
            "CMDM_ID_HASH_KEY is not set. API keys and national identifiers are "
            "stored as keyed digests; an unkeyed digest of either is reversible. "
            'Set one and keep it: export CMDM_ID_HASH_KEY="$(openssl rand -base64 32)"'
        )

    from cmdm.db.engine import apply_migrations
    from cmdm.governance.rbac import create_principal

    wanted = DEFAULT_PRINCIPALS
    if args.principal:
        wanted = {}
        for spec in args.principal:
            subject, _, roles = spec.partition(":")
            if not subject or not roles:
                parser.error(f"{spec!r} is not SUBJECT:ROLE[,ROLE]")
            wanted[subject] = tuple(r.strip().upper() for r in roles.split(","))

    with psycopg.connect(args.dsn) as conn:
        if not args.skip_migrations:
            applied = apply_migrations(conn)
            conn.commit()
            print(f"migrations: {len(applied)} applied" + (
                f" ({', '.join(applied)})" if applied else " (schema already current)"
            ))

        existing = {
            row[0]
            for row in conn.execute("SELECT subject FROM mdm.principal").fetchall()
        }

        print()
        for subject, roles in wanted.items():
            if subject in existing:
                print(f"  {subject:<10} already registered — key not shown; rotate to replace")
                continue
            _, secret = create_principal(conn, subject, roles, kind="SERVICE")
            print(f"  {subject:<10} {','.join(roles):<20} {secret}")
        conn.commit()

    print(
        "\nKeys are shown once. Send one as the X-API-Key header from code, or "
        "paste it into /console/login in a browser.\n"
        "\nNext:\n"
        "  uvicorn cmdm.api.app:app --port 8000     # API + all three consoles\n"
        "  python -m cmdm.worker serve              # drains the ingest queue"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
