"""Database connections and migrations.

Thin on purpose. There is no ORM here and none is wanted: the write paths in
this system are bulk COPY, set-based merges and a queue claim built on
``FOR UPDATE SKIP LOCKED``. An ORM would obscure all three and add a translation
layer over SQL that is already the clearest expression of what these operations
do.

Connections come from a pool because the API serves concurrent requests and each
one needs its own transaction. The pool is created lazily and shared, so
importing this module costs nothing in a process that never touches the
database — which matters because the model registry and the vectorized layer are
imported by workers that have no reason to connect.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

__all__ = [
    "dsn_from_env",
    "connect",
    "transaction",
    "pool",
    "close_pool",
    "apply_migrations",
    "MIGRATIONS_DIR",
]

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"

_pool: ConnectionPool | None = None


def dsn_from_env() -> str:
    """Build a connection string from the environment.

    ``CMDM_DSN`` wins when set. Otherwise the standard ``PG*`` variables are
    used, so the service behaves like any other Postgres client and inherits
    whatever the deployment already configures.

    With neither set, and only then, the embedded instance is used if it is
    installed -- which is what lets an offline install start with nothing
    configured. It is last, not first: a machine that has told us where its
    database is must never be quietly given a different one.
    """
    dsn = os.environ.get("CMDM_DSN")
    if dsn:
        return dsn

    if not any(v in os.environ for v in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE")):
        embedded = _embedded_dsn()
        if embedded:
            return embedded

    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "postgres")
    database = os.environ.get("PGDATABASE", "cmdm")
    password = os.environ.get("PGPASSWORD")
    parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={database}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def _embedded_dsn() -> str | None:
    """The embedded instance's DSN, or None when it is not installed.

    Cached in the environment so that the API server, the worker and a script
    run in the same shell all resolve to the same instance without each paying
    the start-up check.
    """
    from cmdm.embedded import EmbeddedPostgresUnavailable, ensure_dsn

    try:
        dsn = ensure_dsn()
    except EmbeddedPostgresUnavailable:
        # No binaries to run. Fall through to the PG* defaults, which will
        # produce a connection error naming a host -- clearer than a message
        # about an embedded server the deployment never asked for.
        return None
    os.environ["CMDM_DSN"] = dsn
    return dsn


def pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    """Return the shared connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn or dsn_from_env(),
            min_size=min_size,
            max_size=max_size,
            open=True,
            # Fail fast rather than hanging a request forever behind an
            # exhausted pool. A timeout surfaces as a 503 the caller can retry;
            # an indefinite wait surfaces as a mystery.
            timeout=10.0,
        )
    return _pool


def close_pool() -> None:
    """Close the shared pool. For test teardown and clean shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection."""
    with pool(dsn).connection() as conn:
        yield conn


@contextmanager
def transaction(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Borrow a connection and wrap the block in one transaction.

    This is the unit that makes the queue design work. Landing raw records and
    enqueueing the job that will process them happen inside one of these, so
    they commit together or not at all.
    """
    with pool(dsn).connection() as conn, conn.transaction():
        yield conn


#: Marker the DDL renderer writes into the baseline it generates.
GENERATED_MARKER = "GENERATED FILE. Do not edit by hand."


def _is_generated(sql: str) -> bool:
    """Whether this file is rendered from the registry rather than hand-written.

    Read from the file's own header rather than from a filename allow-list, so
    the rule follows the property that justifies it: a file nobody edits by hand
    cannot be edited by hand in a way worth stopping for.
    """
    return GENERATED_MARKER in sql[:1000]


def apply_migrations(
    conn: psycopg.Connection, *, directory: pathlib.Path | None = None
) -> list[str]:
    """Apply every unapplied ``NNN_*.sql`` file in order.

    Deliberately minimal — no down-migrations, no branching. A schema change to
    a system of record is forward-only in practice: rolling one back means
    deciding what happens to the rows written since, which is a data decision
    and not something a migration tool should be trusted to guess.

    Returns the filenames applied.
    """
    directory = directory or MIGRATIONS_DIR

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migration (
                filename    text PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now(),
                checksum    text NOT NULL
            )
            """
        )
        cur.execute("SELECT filename, checksum FROM public.schema_migration")
        applied = dict(cur.fetchall())

    from cmdm.model.ids import payload_hash

    out: list[str] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = payload_hash({"sql": sql})

        if path.name in applied:
            # An edited migration is a bug worth stopping for: the database
            # already ran the old text, so the file no longer describes the
            # schema that exists.
            #
            # The generated baseline is the one exception, and it is not a
            # loophole. 001 is not a migration in the same sense as the others:
            # it is the schema's *definition*, rendered from the field registry
            # and replayed only into an empty database. A registry change
            # necessarily rewrites it, and a store that already applied it moves
            # forward through the numbered hand-written files instead -- so
            # refusing to start because the definition has moved on would fail
            # every existing installation on every release, to protect against a
            # file that is never re-run. What must not drift is the *schema*,
            # and that is what the hand-written migrations carry.
            if applied[path.name] != checksum and not _is_generated(sql):
                raise RuntimeError(
                    f"{path.name} has changed since it was applied. Migrations are "
                    "immutable once run; add a new file instead."
                )
            if applied[path.name] != checksum:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.schema_migration SET checksum = %s "
                        "WHERE filename = %s",
                        (checksum, path.name),
                    )
            continue

        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO public.schema_migration (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
        out.append(path.name)

    return out
