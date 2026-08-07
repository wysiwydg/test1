"""Shared fixtures.

Database tests run against a real PostgreSQL instance, never a mock or SQLite.
Almost everything worth testing in the storage layer is behaviour SQLite does
not have: ``FOR UPDATE SKIP LOCKED``, partial unique indexes as ON CONFLICT
arbiters, deferred foreign keys, enum types, ``jsonb``. A mock would assert that
the code calls the functions it calls, which is not the same as asserting the
database does what the design claims.

When no database is reachable those tests skip rather than fail, so the suite
still runs in an environment without one — but the skip is loud enough to notice
in CI output, because silently skipping the storage tests would be worse than
not having them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg


def _dsn() -> str | None:
    """Resolve a test DSN, preferring an explicit one."""
    return os.environ.get("CMDM_TEST_DSN") or os.environ.get("CMDM_DSN")


@pytest.fixture(scope="session")
def dsn() -> str:
    """A DSN pointing at a reachable database, or skip."""
    candidate = _dsn()
    if not candidate:
        pytest.skip("no CMDM_TEST_DSN / CMDM_DSN set; database tests skipped")

    try:
        import psycopg
    except ImportError:  # pragma: no cover
        pytest.skip("psycopg not installed")

    try:
        with psycopg.connect(candidate, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database not reachable at {candidate!r}: {exc}")

    return candidate


@pytest.fixture(scope="session")
def migrated(dsn: str) -> str:
    """Ensure the schema is present. Session-scoped: migrations are idempotent."""
    import psycopg

    from cmdm.db.engine import apply_migrations

    with psycopg.connect(dsn) as conn:
        apply_migrations(conn)
        conn.commit()
    return dsn


@pytest.fixture()
def conn(migrated: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back at the end of the test.

    Each test runs inside a transaction that is never committed, so tests share
    one database without sharing state and without a truncate step between them.
    """
    import psycopg

    connection = psycopg.connect(migrated)
    try:
        connection.autocommit = False
        yield connection
    finally:
        connection.rollback()
        connection.close()
