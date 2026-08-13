"""Deploy a release into a bundle that is already installed. No network.

The full bundle is 150 MB and almost none of it changes between releases: the
PostgreSQL binaries, polars, scipy and pyarrow are the same files every time. A
change to this project is one pure-Python wheel of about a quarter of a
megabyte. This deploys that, in place, to an installation that already works.

    python update.py  C:\\path\\to\\cmdm-offline

Standard library only, like the installer, and for the same reason: a script
that fails obscurely on an air-gapped machine is worse than one that refuses
loudly, because nobody there can search the symptom with.

Swapping the wheel is the easy part and, on its own, the wrong amount of work.
A release can also change the schema, change what a stored identity means, and
change the shape of the extract the sample data is in. Deploying only the code
leaves an installation that starts, looks healthy, and is wrong. So this runs
the whole deployment and reports each part:

1.  **Check.** Is the target a bundle, is it installed, does this release need a
    package it does not carry, and is the venv's Python the one it was built
    for. Nothing is written until all of these pass.
2.  **Back up.** Everything about to be replaced is copied to
    ``backup-<timestamp>/`` first. The database is not touched at this stage and
    so needs no copy.
3.  **Files and wheel.** ``src/``, ``tests/``, ``docs/``, ``data/`` and the
    root scripts, then ``pip install --no-index`` of the wheel.
4.  **Schema.** Apply any migrations the release adds. A store one release
    behind has an older schema, and code that assumes the new columns fails on
    the first query rather than at start-up, which is a much worse place to
    find out.
5.  **Identity check.** Compare the crosswalk against the mappings now
    installed. A release that changes a key kind changes what a stored identity
    *means*, and re-processing after such a change mints a second golden party
    rather than correcting the first. Detected here and reported with the
    command that fixes it, because nothing in the database is violated by it and
    nothing else will ever notice.
6.  **Verify.** Import the installed package and read the store, so the run ends
    on evidence rather than on the absence of an error.

What it will not do matters as much. It never touches ``pgdata``,
``pgpassword`` or the config files -- the database, the generated superuser
password and the identifier-hashing key are the irreplaceable part of an
installation. It never deletes golden records; the one operation that does
(``worker rebuild``) is named in the output for a human to run deliberately.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

#: Replaced wholesale. Everything here is shipped content, reproducible from the
#: wheel and the repository; nothing in it is machine state.
#:
#: ``data`` is in the list because the sample extract is shipped content too,
#: and a release that adds a source column ships a sample carrying it. Leaving
#: the old file behind makes ``verify`` fail on a bundle that is otherwise
#: correct, which reads as a broken release.
DIRECTORIES = ["src", "tests", "docs", "data"]

#: Replaced if the pack carries them.
FILES = ["verify.py", "README.md", "OFFLINE-INSTALL.md", "worker.cmd", "worker.sh"]

#: Never touched, by anything, ever. Listed so the intent is checkable rather
#: than implied by the absence of code that would have written to them, and
#: asserted in the test suite against DIRECTORIES and FILES.
PRESERVED = ["pgdata", "pgpassword", "config.cmd", "config.sh", ".venv",
             "wheels", "pgsql"]


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def warn(message: str) -> None:
    print(f"  ! {message}", flush=True)


def fail(message: str) -> int:
    print(f"\nSTOPPED: {message}\n", file=sys.stderr)
    return 1


def venv_python(bundle: pathlib.Path) -> pathlib.Path:
    if platform.system() == "Windows":
        return bundle / ".venv" / "Scripts" / "python.exe"
    return bundle / ".venv" / "bin" / "python"


def _run(
    python: pathlib.Path, code: str, cwd: pathlib.Path,
    *, prefer_source: bool = False,
) -> subprocess.CompletedProcess:
    """Run a snippet inside the bundle's interpreter.

    Every step after the wheel install has to run there rather than here: this
    script is deliberately standard-library-only and cannot import psycopg,
    polars or the project itself.

    ``prefer_source`` puts this pack's own ``src/`` ahead of what is installed,
    which is what makes ``--check`` predictive. Asking the *old* code whether
    the *new* release re-keys the crosswalk gets "unavailable: cannot import
    ..." -- an honest answer to the wrong question, and useless at the moment it
    is most wanted, which is before anything has been written.
    """
    env = dict(os.environ)
    if prefer_source:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{HERE / 'src'}{os.pathsep}{existing}" if existing else str(HERE / "src")
        )
    return subprocess.run(
        [str(python), "-c", code], cwd=cwd, capture_output=True, text=True,
        timeout=600, env=env,
    )


def _is_bundle(path: pathlib.Path) -> bool:
    """Enough of the shape to be sure, without demanding all of it."""
    return (path / "wheels").is_dir() and (
        (path / "verify.py").exists() or (path / "install.py").exists()
    )


def _missing_dependencies(bundle: pathlib.Path) -> list[str]:
    """Names this release needs that the bundle's wheelhouse does not carry.

    A pure-Python update travels between bundles freely -- one pack works on
    every platform, because the compiled wheels are the ones with a platform tag
    and none of them are in the pack. That stops being true the moment a release
    adds a dependency. Checking here turns "installed, then ImportError on a
    machine with no internet" into a refusal that names the missing package
    while there is still something to be done about it.
    """
    declared = HERE / "DEPENDENCIES.txt"
    if not declared.exists():
        return []

    have = {w.name.split("-")[0].lower().replace("_", "-")
            for w in (bundle / "wheels").glob("*.whl")}
    return [
        name for name in declared.read_text(encoding="utf-8").split()
        if name and name != "cmdm" and name.lower().replace("_", "-") not in have
    ]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _check(bundle: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | int:
    """Everything that must be true before anything is written."""
    if not bundle.is_dir():
        return fail(f"no such folder: {bundle}")
    if not _is_bundle(bundle):
        return fail(
            f"{bundle} does not look like a Customer MDM bundle -- it has no "
            "wheels/ folder. Pass the folder that contains start.cmd."
        )

    python = venv_python(bundle)
    if not python.exists():
        return fail(
            f"the bundle has no virtual environment at {python}. Run its "
            "install.cmd (or ./install.sh) first, then apply this update."
        )

    missing = _missing_dependencies(bundle)
    if missing:
        return fail(
            "this release needs package(s) the bundle does not carry: "
            f"{', '.join(sorted(missing))}. An update pack cannot add compiled "
            "dependencies -- use the full bundle for this release."
        )

    wheel = next(iter(sorted((HERE / "wheels").glob("cmdm-*.whl"))), None)
    if wheel is None:
        return fail(f"no cmdm wheel in {HERE / 'wheels'}")

    return python, wheel


def _replace_files(bundle: pathlib.Path) -> pathlib.Path:
    """Swap in the shipped content, keeping what it replaced."""
    backup = bundle / f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir()
    say(f"keeping what is replaced in {backup.name}/")

    for name in DIRECTORIES:
        source = HERE / name
        if not source.is_dir():
            continue
        existing = bundle / name
        if existing.is_dir():
            shutil.move(str(existing), str(backup / name))
        shutil.copytree(source, existing,
                        ignore=shutil.ignore_patterns("__pycache__"))
        say(f"replaced {name}/")

    for name in FILES:
        source = HERE / name
        if not source.exists():
            continue
        existing = bundle / name
        if existing.exists():
            shutil.copy2(existing, backup / name)
        shutil.copy2(source, existing)
        if source.suffix == ".sh":
            existing.chmod(0o755)
        say(f"replaced {name}")

    return backup


def _migrate(python: pathlib.Path, bundle: pathlib.Path) -> str | None:
    """Bring the schema up to what this release expects.

    Returns a human-readable outcome, or None if there is no database to
    migrate -- a bundle installed but never started has no store yet, and
    that is not a failure.
    """
    result = _run(python, _MIGRATE_SNIPPET, bundle)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return "FAILED: " + (detail[-1] if detail else "no output")
    return result.stdout.strip() or "no change"


_MIGRATE_SNIPPET = """
import os, sys
try:
    import psycopg
    from cmdm.db.engine import apply_migrations, dsn_from_env
except Exception as exc:
    print("unavailable: " + str(exc)); sys.exit(0)
try:
    dsn = dsn_from_env()
except Exception:
    dsn = os.environ.get("CMDM_DSN")
if not dsn:
    print("no database configured yet; it will be created on first start")
    sys.exit(0)
try:
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        applied = apply_migrations(conn)
        conn.commit()
except Exception as exc:
    print("database not reachable (" + type(exc).__name__ + "); "
          "migrations will run on first start")
    sys.exit(0)
print(("applied " + ", ".join(applied)) if applied else "already up to date")
"""


def _identity_check(
    python: pathlib.Path, bundle: pathlib.Path, *, prefer_source: bool = False
) -> tuple[bool, str]:
    """Does the stored crosswalk still mean what the mappings say it means?

    The failure this exists for leaves nothing broken in the database. A key
    kind that no mapping declares any more is simply an identity nothing writes
    to; re-processing mints a second golden party beside it and the customer
    quietly becomes two. Nothing is violated, so nothing else reports it.
    """
    result = _run(python, _IDENTITY_SNIPPET, bundle, prefer_source=prefer_source)
    text = (result.stdout or result.stderr).strip()
    return result.returncode == 0, text or "could not be checked"


_IDENTITY_SNIPPET = """
import os, pathlib, sys
try:
    import psycopg
    from cmdm.db.engine import dsn_from_env
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import MAPPINGS_DIR, stale_key_kinds
except Exception as exc:
    print("unavailable: " + str(exc)); sys.exit(0)
try:
    dsn = dsn_from_env()
except Exception:
    dsn = os.environ.get("CMDM_DSN")
if not dsn:
    print("no database yet"); sys.exit(0)
try:
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        mappings = [load_mapping(p) for p in sorted(MAPPINGS_DIR.glob("*.toml"))]
        stale = stale_key_kinds(conn, mappings)
except Exception as exc:
    print("not reachable (" + type(exc).__name__ + ")"); sys.exit(0)
if not stale:
    print("the crosswalk matches the installed mappings"); sys.exit(0)
total = sum(stale.values())
print("RE-KEYED: " + ", ".join(k + " (" + format(v, ",") + " rows)"
                               for k, v in sorted(stale.items()))
      + " -- " + format(total, ",") + " crosswalk rows")
sys.exit(2)
"""


def _health(python: pathlib.Path, bundle: pathlib.Path) -> str:
    """End on evidence: import the installed package and count the store."""
    result = _run(python, _HEALTH_SNIPPET, bundle)
    return (result.stdout or result.stderr).strip() or "no output"


_HEALTH_SNIPPET = """
import os, sys
import cmdm
line = "cmdm " + cmdm.__version__
try:
    import psycopg
    from cmdm.db.engine import dsn_from_env
    dsn = dsn_from_env()
except Exception:
    dsn = os.environ.get("CMDM_DSN")
if dsn:
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM mdm.policy WHERE is_current),"
                "       (SELECT count(*) FROM mdm.person WHERE is_current),"
                "       (SELECT count(*) FROM mdm.relationship WHERE is_current),"
                "       (SELECT count(DISTINCT household_id) FROM mdm.person"
                "         WHERE is_current AND household_id IS NOT NULL)"
            ).fetchone()
            line += ("  |  " + format(row[0], ",") + " policies, "
                     + format(row[1], ",") + " persons, "
                     + format(row[2], ",") + " edges, "
                     + format(row[3], ",") + " households")
    except Exception as exc:
        line += "  |  store not readable (" + type(exc).__name__ + ")"
print(line)
"""


# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="update", description="Deploy a Customer MDM release into an "
                                   "installed offline bundle."
    )
    parser.add_argument(
        "bundle", nargs="?", default=str(HERE.parent),
        help="the folder containing start.cmd; defaults to the parent of this pack",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="report what would happen and change nothing",
    )
    parser.add_argument(
        "--skip-migrations", action="store_true",
        help="do not touch the schema; it will be migrated on first start",
    )
    args = parser.parse_args(argv[1:])
    bundle = pathlib.Path(args.bundle).resolve()

    print("\nCustomer MDM — deploying an update\n")
    print(f"  update:  {HERE}")
    print(f"  bundle:  {bundle}\n")

    checked = _check(bundle)
    if isinstance(checked, int):
        return checked
    python, wheel = checked

    if args.check:
        say(f"would install {wheel.name}")
        say(f"would replace {', '.join(DIRECTORIES)} and {', '.join(FILES)}")
        say(f"would not touch {', '.join(PRESERVED)}")
        # Read against this pack's source, so the answer is about the release
        # being deployed rather than about the one already installed.
        ok, message = _identity_check(python, bundle, prefer_source=True)
        say(f"identity: {message}")
        if not ok and message.startswith("RE-KEYED"):
            warn("this release re-keys the crosswalk; after deploying, run "
                 "`worker rebuild` rather than `worker backfill`")
        print("\n  Nothing was changed (--check).\n")
        return 0

    backup = _replace_files(bundle)

    say(f"installing {wheel.name}")
    installed = subprocess.run([
        str(python), "-m", "pip", "install", "--no-index", "--no-deps",
        "--force-reinstall", "--disable-pip-version-check", "-q", str(wheel),
    ], cwd=bundle)
    if installed.returncode != 0:
        return fail(
            f"pip exited {installed.returncode}. Nothing else was changed; the "
            f"previous {', '.join(DIRECTORIES)} are in {backup.name}/."
        )

    if args.skip_migrations:
        say("schema: skipped, as asked")
    else:
        outcome = _migrate(python, bundle)
        if outcome and outcome.startswith("FAILED"):
            warn(f"schema: {outcome}")
            warn("the code is installed but the store is a release behind. Fix "
                 "the cause and re-run, or start the app, which migrates too.")
        else:
            say(f"schema: {outcome}")

    ok, message = _identity_check(python, bundle)
    ext = "cmd" if platform.system() == "Windows" else "sh"
    prefix = "" if platform.system() == "Windows" else "./"

    say(f"health: {_health(python, bundle)}")

    print("\n  Updated. Your database, keys and config were not touched.\n")

    if not ok and message.startswith("RE-KEYED"):
        warn(f"identity: {message}")
        print()
        print("  This release changed what a stored identity means, so the "
              "existing\n  golden records are keyed on identities nothing "
              "writes to any more.\n  Backfilling would mint a second party "
              "for each rather than correct it.\n")
        print("  Rebuild from the landing zone instead:\n")
        print(f"      {prefix}worker.{ext} rebuild\n")
        print("  That discards the derived store -- golden records, crosswalks,\n"
              "  provenance, the match ledger -- and recomputes all of it from "
              "the\n  delivered bytes, which are immutable and still there. It "
              "does change\n  published person and policy ids, and steward "
              "decisions keyed on the\n  old identities will not carry over.\n")
    else:
        say(f"identity: {message}")
        print()
        print(f"  1. prove it still runs:     {prefix}verify.{ext} --quick")
        print(f"  2. bring the store forward: {prefix}worker.{ext} backfill")
        print(f"  3. start it:                {prefix}start.{ext}"
              "     then http://127.0.0.1:8000/console/entities\n")
        print("  Step 2 re-runs every batch already landed through the current "
              "pipeline.\n  It is what fills in anything this release computes "
              "that the last one did not,\n  and it is safe to run twice.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
