"""A PostgreSQL instance the deployment does not have to install.

This exists for one situation and is honest about it: a machine with no
database, no administrator rights and no internet. That is the normal state of
an air-gapped insurance environment, and "first install PostgreSQL 16" is not an
answer there.

    python -m cmdm.embedded start     # init if needed, start, create the db
    python -m cmdm.embedded dsn       # print the DSN, starting it if needed
    python -m cmdm.embedded stop
    python -m cmdm.embedded status

It is a real PostgreSQL server -- the same binaries, the same version -- run out
of a directory rather than installed system-wide. Nothing about the golden store
is weakened: the schema, the ``FOR UPDATE SKIP LOCKED`` queue, the partial
unique indexes and the enum types are all exactly what they are against a
system instance. Only the lifecycle differs.

**It does not take over.** ``CMDM_DSN`` always wins. A deployment with its own
instance sets that variable and this module is never imported, which is why
``pgserver`` is an optional dependency rather than a required one.

The data directory defaults to ``./pgdata`` under :data:`CMDM_HOME`, so an
operator can see it, back it up, and delete it to start over -- rather than it
living somewhere under a temp directory that the next reboot clears.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from collections.abc import Sequence

__all__ = ["data_dir", "ensure_running", "ensure_dsn", "stop", "status", "main"]

#: Where the embedded instance keeps its data. Overridable, and deliberately a
#: visible path rather than a temp directory: this is the golden store.
HOME_ENV = "CMDM_HOME"
DATA_DIR_ENV = "CMDM_PGDATA"

#: The database the schema is applied to inside the embedded instance.
DATABASE = "cmdm"


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get(HOME_ENV) or pathlib.Path.cwd()).resolve()


def data_dir() -> pathlib.Path:
    override = os.environ.get(DATA_DIR_ENV)
    return pathlib.Path(override).resolve() if override else home() / "pgdata"


def _server(cleanup_mode: str | None = None):
    """Get the pgserver handle, initialising and starting on first use.

    ``cleanup_mode=None`` deliberately: the default stops the server when the
    process that started it exits, which is wrong here. The API server and the
    worker are separate processes sharing one database, and whichever happened
    to start it would take the database down with it when it stopped.
    """
    try:
        import pgserver
    except ImportError:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "the embedded PostgreSQL is not installed. Either install it "
            "(pip install 'cmdm[embedded]') or point CMDM_DSN at a PostgreSQL "
            "instance you already have."
        ) from None

    target = data_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(target, cleanup_mode=cleanup_mode)


def ensure_running() -> str:
    """Start the embedded server if it is not up, and return its base URI."""
    return _server().get_uri()


def ensure_dsn() -> str:
    """Return a DSN for the ``cmdm`` database, creating the database if needed.

    ``CMDM_DSN`` short-circuits this entirely, which is what makes the embedded
    server an opt-in convenience rather than a fork in how the system connects.
    """
    existing = os.environ.get("CMDM_DSN")
    if existing:
        return existing

    server = _server()
    base = server.get_uri()

    # `psycopg` is a hard dependency of the store extra, and this module is only
    # reached by something that is about to connect.
    import psycopg

    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
        ).fetchone()
        if not exists:
            # Identifier interpolated rather than parameterised because CREATE
            # DATABASE takes no parameters; DATABASE is a module constant, not
            # anything a caller supplies.
            conn.execute(f'CREATE DATABASE "{DATABASE}"')

    return server.get_uri(database=DATABASE)


def stop() -> None:
    """Stop the embedded server, leaving its data directory intact."""
    try:
        import pgserver
    except ImportError:  # pragma: no cover
        return
    target = data_dir()
    if not (target / "postmaster.pid").exists():
        return
    server = pgserver.get_server(target, cleanup_mode=None)
    server.cleanup()


def status() -> dict[str, object]:
    """Whether the embedded instance exists and is running."""
    target = data_dir()
    return {
        "data_dir": str(target),
        "initialised": (target / "PG_VERSION").exists(),
        "running": (target / "postmaster.pid").exists(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cmdm.embedded",
        description="The self-contained PostgreSQL instance.",
    )
    parser.add_argument(
        "command", choices=("start", "dsn", "stop", "status"), nargs="?", default="status"
    )
    args = parser.parse_args(argv)

    if args.command == "stop":
        stop()
        print("stopped")
        return 0

    if args.command == "status":
        for key, value in status().items():
            print(f"{key}: {value}")
        return 0

    dsn = ensure_dsn()
    if args.command == "dsn":
        # Bare, on stdout, so a shell can capture it:
        #   for /f %i in ('python -m cmdm.embedded dsn') do set CMDM_DSN=%i
        print(dsn)
    else:
        print(f"started\ndata_dir: {data_dir()}\nCMDM_DSN={dsn}", file=sys.stderr)
        print(dsn)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
