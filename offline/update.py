"""Apply an update pack to a bundle that is already installed. No network.

The full bundle is 150 MB and almost none of it changes between releases: the
PostgreSQL binaries, polars, scipy and pyarrow are the same files every time. A
change to this project is one pure-Python wheel of about a quarter of a
megabyte. This applies that, in place, to an installation that already works.

    python update.py  C:\\path\\to\\cmdm-offline

Standard library only, like the installer, and for the same reason: a script
that fails obscurely on an air-gapped machine is worse than one that refuses
loudly, because nobody there can search the symptom.

What it will not do is more important than what it will. It refuses a target
that is not a Customer MDM bundle, refuses an update needing a dependency the
bundle does not already have, and refuses to touch pgdata, config.cmd or
config.sh -- the database, the identifier-hashing key and the API keys are the
irreplaceable part of an installation and no update has any business there.
"""

from __future__ import annotations

import pathlib
import platform
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

#: Replaced wholesale. Everything here is shipped content, reproducible from
#: the wheel and the repository; nothing in it is machine state.
DIRECTORIES = ["src", "tests", "docs"]

#: Replaced if the pack carries them.
FILES = ["verify.py", "README.md", "OFFLINE-INSTALL.md"]

#: Never touched, by anything, ever. Listed so the intent is checkable rather
#: than implied by the absence of code that would have written to them.
PRESERVED = ["pgdata", "config.cmd", "config.sh", ".venv", "wheels", "pgsql", "data"]


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def fail(message: str) -> int:
    print(f"\nSTOPPED: {message}\n", file=sys.stderr)
    return 1


def venv_python(bundle: pathlib.Path) -> pathlib.Path:
    if platform.system() == "Windows":
        return bundle / ".venv" / "Scripts" / "python.exe"
    return bundle / ".venv" / "bin" / "python"


def _is_bundle(path: pathlib.Path) -> bool:
    """Enough of the shape to be sure, without demanding all of it."""
    return (path / "wheels").is_dir() and (
        (path / "verify.py").exists() or (path / "install.py").exists()
    )


def _missing_dependencies(bundle: pathlib.Path) -> list[str]:
    """Names this release needs that the bundle's wheelhouse does not carry.

    A pure-Python update travels between bundles freely -- one pack works on
    every platform, because the compiled wheels are the ones with a platform
    tag and none of them are in the pack. That stops being true the moment a
    release adds a dependency. Checking here turns "it installed and then
    ImportError'd on a machine with no internet" into a refusal that names the
    missing package while there is still something to be done about it.
    """
    declared = HERE / "DEPENDENCIES.txt"
    if not declared.exists():
        return []

    have = set()
    for wheel in (bundle / "wheels").glob("*.whl"):
        have.add(wheel.name.split("-")[0].lower().replace("_", "-"))

    return [
        name for name in declared.read_text(encoding="utf-8").split()
        if name and name != "cmdm" and name.lower().replace("_", "-") not in have
    ]


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[1]).resolve() if len(argv) > 1 else HERE.parent

    print("\nCustomer MDM — applying an update\n")
    print(f"  update:  {HERE}")
    print(f"  bundle:  {target}\n")

    if not target.is_dir():
        return fail(f"no such folder: {target}")
    if not _is_bundle(target):
        return fail(
            f"{target} does not look like a Customer MDM bundle -- it has no "
            "wheels/ folder. Pass the folder that contains start.cmd."
        )

    python = venv_python(target)
    if not python.exists():
        return fail(
            f"the bundle has no virtual environment at {python}. Run its "
            "install.cmd (or ./install.sh) first, then apply this update."
        )

    missing = _missing_dependencies(target)
    if missing:
        return fail(
            "this release needs package(s) the bundle does not carry: "
            f"{', '.join(sorted(missing))}. An update pack cannot add compiled "
            "dependencies -- use the full bundle for this release."
        )

    wheel = next(iter(sorted((HERE / "wheels").glob("cmdm-*.whl"))), None)
    if wheel is None:
        return fail(f"no cmdm wheel in {HERE / 'wheels'}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target / f"backup-{stamp}"
    backup.mkdir()
    say(f"keeping what is replaced in {backup.name}/")

    for name in DIRECTORIES:
        source = HERE / name
        if not source.is_dir():
            continue
        existing = target / name
        if existing.is_dir():
            shutil.move(str(existing), str(backup / name))
        shutil.copytree(source, existing,
                        ignore=shutil.ignore_patterns("__pycache__"))
        say(f"replaced {name}/")

    for name in FILES:
        source = HERE / name
        if not source.exists():
            continue
        existing = target / name
        if existing.exists():
            shutil.copy2(existing, backup / name)
        shutil.copy2(source, existing)
        say(f"replaced {name}")

    say(f"installing {wheel.name}")
    result = subprocess.run([
        str(python), "-m", "pip", "install", "--no-index", "--no-deps",
        "--force-reinstall", "--disable-pip-version-check", "-q", str(wheel),
    ], cwd=target)
    if result.returncode != 0:
        return fail(
            f"pip exited {result.returncode}. Nothing else was changed; the "
            f"previous src/, tests/ and docs/ are in {backup.name}/."
        )

    installed = subprocess.run(
        [str(python), "-c", "import cmdm; print(cmdm.__version__)"],
        cwd=target, capture_output=True, text=True,
    )
    version = installed.stdout.strip() or "unknown"

    ext = "cmd" if platform.system() == "Windows" else "sh"
    prefix = "" if platform.system() == "Windows" else "./"
    print(f"\n  Updated to {version}. Your database, keys and config were not "
          "touched.\n")
    print(f"  1. prove it still runs:   {prefix}verify.{ext} --quick")
    print(f"  2. bring the store forward: {prefix}worker.{ext} backfill")
    print(f"  3. start it:              {prefix}start.{ext}"
          "     then open http://127.0.0.1:8000/console/entities\n")
    print("  Step 2 re-runs every batch already landed through the current "
          "pipeline.\n  It is what fills in anything this release computes "
          "that the last one did not,\n  and it is safe to run twice.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
