"""A PostgreSQL instance the deployment does not have to install.

This exists for one situation and is honest about it: a machine with no
database, no administrator rights and no internet. That is the normal state of
an air-gapped insurance environment, and "first install PostgreSQL 16" is not an
answer there.

    python -m cmdm.embedded start     # init if needed, start, create the db
    python -m cmdm.embedded dsn       # print the DSN, starting it if needed
    python -m cmdm.embedded stop
    python -m cmdm.embedded status

It drives the real ``initdb`` and ``pg_ctl`` as subprocesses rather than
wrapping them in a Python extension. That is not a stylistic choice: a database
engine has nothing to do with CPython's ABI, and binding it to one means the
whole system inherits whatever Python versions somebody else's wheel happened to
be built for. Shipping the binaries and calling them keeps the server usable on
any interpreter.

It is a real PostgreSQL server -- the same binaries, the same version -- run out
of a directory rather than installed system-wide. Nothing about the golden store
is weakened: the schema, the ``FOR UPDATE SKIP LOCKED`` queue, the partial
unique indexes and the enum types are all exactly what they are against a system
instance. Only the lifecycle differs.

**It does not take over.** ``CMDM_DSN`` always wins. A deployment with its own
instance sets that variable and none of this runs.

The data directory defaults to ``./pgdata`` under :data:`CMDM_HOME`, so an
operator can see it, back it up, and delete it to start over -- rather than it
living under a temp directory that the next reboot clears.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence

__all__ = [
    "binaries",
    "data_dir",
    "ensure_running",
    "ensure_dsn",
    "stop",
    "status",
    "main",
]

HOME_ENV = "CMDM_HOME"
DATA_DIR_ENV = "CMDM_PGDATA"
BIN_ENV = "CMDM_PG_BIN"

#: The database the schema is applied to inside the embedded instance.
DATABASE = "cmdm"

#: How long to wait for the server to accept connections after pg_ctl returns.
START_TIMEOUT_SECONDS = 30.0

WINDOWS = platform.system() == "Windows"
EXE = ".exe" if WINDOWS else ""


class EmbeddedPostgresUnavailable(RuntimeError):
    """No PostgreSQL binaries could be found to run."""


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get(HOME_ENV) or pathlib.Path.cwd()).resolve()


def data_dir() -> pathlib.Path:
    override = os.environ.get(DATA_DIR_ENV)
    return pathlib.Path(override).resolve() if override else home() / "pgdata"


def binaries() -> pathlib.Path:
    """Locate ``initdb`` and ``pg_ctl``.

    Four places, most explicit first. The offline bundle ships ``pgsql/bin``
    beside itself; a developer machine usually has ``pgserver`` installed from
    PyPI; and a machine with PostgreSQL already installed has them on PATH.
    """
    override = os.environ.get(BIN_ENV)
    if override:
        candidate = pathlib.Path(override).resolve()
        if (candidate / f"initdb{EXE}").exists():
            return candidate
        raise EmbeddedPostgresUnavailable(
            f"{BIN_ENV} is set to {candidate}, which contains no initdb{EXE}"
        )

    bundled = home() / "pgsql" / "bin"
    if (bundled / f"initdb{EXE}").exists():
        return bundled

    try:
        import pgserver

        vendored = pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (vendored / f"initdb{EXE}").exists():
            return vendored
    except ImportError:
        pass

    found = shutil.which("initdb")
    if found:
        return pathlib.Path(found).resolve().parent

    raise EmbeddedPostgresUnavailable(
        "no PostgreSQL binaries found. The offline bundle ships them in "
        f"pgsql/bin; otherwise set {BIN_ENV} to a directory containing "
        f"initdb{EXE} and pg_ctl{EXE}, or point CMDM_DSN at a PostgreSQL "
        "instance you already have."
    )


def _environment() -> dict[str, str]:
    """The environment the PostgreSQL binaries run under.

    On Windows the DLLs sit beside the executables and this is a no-op. On
    other platforms the shared libraries are one directory over, and a copied
    installation that does not say so fails with "error while loading shared
    libraries" -- which names a library rather than the missing search path,
    and sends the reader after the wrong thing.
    """
    env = dict(os.environ)
    if WINDOWS:
        return env
    lib = binaries().parent / "lib"
    if lib.is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{existing}" if existing else str(lib)
    return env


def _run(
    program: str, *args: str, check: bool = True, timeout: float = 120.0
) -> subprocess.CompletedProcess:
    """Run one PostgreSQL binary and return its output.

    Output goes to temporary *files*, never to pipes, and this is the whole
    reason the function exists rather than a bare ``subprocess.run``.

    ``pg_ctl start`` launches the postmaster and returns, but the postmaster is
    a child that inherits whatever handles it was given and keeps them for its
    entire life. Give it the write end of a pipe -- which ``capture_output``
    does -- and the parent waits for an end-of-file that will not arrive until
    the database shuts down. The server starts perfectly and the caller hangs
    forever, which is indistinguishable from a server that failed to start.

    A file handle inherited the same way is harmless: nothing waits on it, and
    the contents are read back after the process exits.
    """
    executable = binaries() / f"{program}{EXE}"

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out, \
         tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        try:
            completed = subprocess.run(
                [str(executable), *args],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=_environment(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            out.seek(0)
            err.seek(0)
            raise RuntimeError(
                f"{program} did not finish within {timeout:.0f}s.\n"
                f"{out.read().strip()}\n{err.read().strip()}".strip()
            ) from None
        out.seek(0)
        err.seek(0)
        result = subprocess.CompletedProcess(
            completed.args, completed.returncode, out.read(), err.read()
        )

    if check and result.returncode != 0:
        raise RuntimeError(
            f"{program} failed (exit {result.returncode})\n"
            f"{result.stdout.strip()}\n{result.stderr.strip()}".strip()
        )
    return result


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_file() -> pathlib.Path:
    return data_dir() / "cmdm_port"


def _password_file() -> pathlib.Path:
    # Beside the data directory rather than inside it, so that deleting pgdata
    # to start over does not leave a password behind that matches nothing.
    return data_dir().parent / "pgpassword"


def _initialise() -> None:
    """Create the cluster, if it is not already there."""
    target = data_dir()
    if (target / "PG_VERSION").exists():
        return

    if not WINDOWS and hasattr(os, "geteuid") and os.geteuid() == 0:
        # PostgreSQL refuses to initialise a cluster as root, and says so in a
        # way that reads as a bug in this program rather than a deliberate
        # refusal by initdb. Windows is unaffected: pg_ctl there starts the
        # server under a restricted token by itself.
        raise EmbeddedPostgresUnavailable(
            "PostgreSQL will not create a data directory as root, and this "
            "process is running as root. Run it as an ordinary user, or set "
            "CMDM_DSN to point at a PostgreSQL instance you already have."
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    # Windows has no Unix sockets, so the server is reachable over TCP, and
    # trust authentication there would let any local account read the whole
    # customer book. A generated password costs nothing and closes that.
    password = secrets.token_urlsafe(24)
    pwfile = _password_file()
    pwfile.write_text(password, encoding="utf-8")
    if not WINDOWS:
        pwfile.chmod(0o600)

    try:
        _run(
            "initdb",
            "-D", str(target),
            "-U", "postgres",
            "--encoding=UTF8",
            "--locale=C",
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
            f"--pwfile={pwfile}",
        )
    except Exception:
        # A half-initialised directory makes every later run fail with
        # "directory is not empty" instead of retrying, so it does not survive
        # a failure.
        shutil.rmtree(target, ignore_errors=True)
        raise


def _is_running() -> bool:
    if not (data_dir() / "postmaster.pid").exists():
        return False
    result = _run("pg_ctl", "-D", str(data_dir()), "status", check=False)
    return result.returncode == 0


def _running_port() -> int | None:
    """The port of a server that is already up.

    Read from the port file, and failing that from ``postmaster.pid``, whose
    fourth line PostgreSQL writes for exactly this purpose. The fallback is not
    theoretical: an interrupted start leaves a running server and no port file,
    and without this the next attempt tries to start a second one on top of it
    and fails with "another server might be running" -- blaming the operator
    for a state the previous run created.
    """
    if not _is_running():
        return None

    if _port_file().exists():
        try:
            return int(_port_file().read_text(encoding="utf-8").strip())
        except ValueError:
            pass

    pid_file = data_dir() / "postmaster.pid"
    try:
        lines = pid_file.read_text(encoding="utf-8").splitlines()
        port = int(lines[3].strip())
    except (OSError, IndexError, ValueError):
        return None

    _port_file().write_text(str(port), encoding="utf-8")
    return port


def ensure_running() -> int:
    """Start the server if it is not up. Returns the port it is listening on."""
    _initialise()
    target = data_dir()

    already = _running_port()
    if already is not None:
        return already

    port = _free_port()
    options = f"-p {port} -h 127.0.0.1"
    if not WINDOWS:
        # A socket in the data directory keeps a local instance off TCP
        # entirely, which Windows cannot do.
        options += f" -k {target}"

    _run(
        "pg_ctl",
        "-D", str(target),
        "-l", str(target / "server.log"),
        "-o", options,
        "-w",                      # wait for it to accept connections
        "start",
    )
    _port_file().write_text(str(port), encoding="utf-8")
    _await_ready(port)
    return port


def _await_ready(port: int) -> None:
    """``pg_ctl -w`` usually suffices; this covers the case where it does not.

    A server that is up but not yet accepting connections produces a confusing
    failure several frames away, in whatever tried to connect first.
    """
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        result = _run(
            "pg_isready", "-h", "127.0.0.1", "-p", str(port), "-q", check=False
        )
        if result.returncode == 0:
            return
        last = (result.stderr or result.stdout).strip()
        time.sleep(0.25)
    log = data_dir() / "server.log"
    detail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
    raise RuntimeError(
        f"the embedded PostgreSQL did not accept connections within "
        f"{START_TIMEOUT_SECONDS:.0f}s. {last}\n{detail}"
    )


def base_dsn(port: int, database: str = "postgres") -> str:
    password = _password_file()
    parts = [f"port={port}", "user=postgres", f"dbname={database}"]
    if WINDOWS:
        parts.append("host=127.0.0.1")
        if password.exists():
            parts.append(f"password={password.read_text(encoding='utf-8').strip()}")
    else:
        parts.append(f"host={data_dir()}")
    return " ".join(parts)


def ensure_dsn() -> str:
    """Return a DSN for the ``cmdm`` database, creating the database if needed.

    ``CMDM_DSN`` short-circuits this entirely, which is what makes the embedded
    server an opt-in convenience rather than a fork in how the system connects.
    """
    existing = os.environ.get("CMDM_DSN")
    if existing:
        return existing

    port = ensure_running()

    import psycopg

    with psycopg.connect(base_dsn(port), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
        ).fetchone()
        if not exists:
            # Identifier interpolated because CREATE DATABASE takes no
            # parameters; DATABASE is a module constant, not caller input.
            conn.execute(f'CREATE DATABASE "{DATABASE}"')

    return base_dsn(port, DATABASE)


def stop() -> None:
    """Stop the server, leaving its data directory intact."""
    if not (data_dir() / "postmaster.pid").exists():
        return
    _run("pg_ctl", "-D", str(data_dir()), "-m", "fast", "-w", "stop", check=False)
    _port_file().unlink(missing_ok=True)


def status() -> dict[str, object]:
    """Whether the embedded instance exists and is running."""
    target = data_dir()
    try:
        where: object = str(binaries())
    except EmbeddedPostgresUnavailable as exc:
        where = f"not found ({exc})"
    return {
        "binaries": where,
        "data_dir": str(target),
        "initialised": (target / "PG_VERSION").exists(),
        "running": _is_running(),
        # Via _running_port so an interrupted start, which leaves a server up
        # and no port file, still reports the port it is actually on.
        "port": _running_port(),
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
        print(f"started\ndata_dir: {data_dir()}", file=sys.stderr)
        print(dsn)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
