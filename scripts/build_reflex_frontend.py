"""Compile the Reflex console into static files a target can serve.

    python -m scripts.build_reflex_frontend

Reflex builds a Vite/React frontend. That needs Node and roughly 200 MB of npm
packages, and the machines this system is deployed on have neither Node nor
internet. The resolution is that none of it belongs on the target: the compile
happens *here*, where the bundle is built, and what ships is the output --
around 650 KB of HTML, JS and CSS. The Reflex backend that serves it is pure
Python and needs no Node at all, which was verified by running it with Node
removed from PATH before any of this was written.

So this script is a build-host tool, deliberately not part of the runtime. It
runs where there is a toolchain and writes into ``src/cmdm/rxui_static/``, which
is packaged into the wheel like any other data file.

**If Node is missing it says so and stops.** It does not fall back to shipping
an empty directory: a bundle whose Reflex console silently serves nothing is
worse than one built without the Reflex console at all, because the failure only
appears on the machine that cannot fix it.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent
RXAPP = REPO / "rxapp"

#: Where the compiled frontend lands. Inside the package so it travels with the
#: wheel -- an update pack ships Python files, and this has to be one of the
#: things it can replace.
TARGET = REPO / "src" / "cmdm" / "rxui_static"

#: What Reflex writes. Its build directory has moved between releases, so the
#: candidates are tried in order rather than one path being assumed correct.
BUILD_DIRS = [
    RXAPP / ".web" / "build" / "client",
    RXAPP / ".web" / "_static",
]


def _toolchain_available() -> str | None:
    """Whichever JS runtime Reflex can drive, or None."""
    for name in ("bun", "node"):
        found = shutil.which(name)
        if found:
            return found
    # Reflex keeps its own copies out of PATH.
    for candidate in (
        pathlib.Path.home() / ".bun" / "bin" / "bun",
        pathlib.Path.home() / ".local" / "share" / "reflex" / "bun" / "bin" / "bun",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def build(*, keep_going: bool = False) -> int:
    if not RXAPP.is_dir():
        print(f"no Reflex app at {RXAPP}", file=sys.stderr)
        return 1

    try:
        import reflex  # noqa: F401
    except ImportError:
        print(
            "reflex is not installed in this interpreter. Install the build "
            "extra first:\n    pip install -e '.[ui]'",
            file=sys.stderr,
        )
        return 0 if keep_going else 1

    if _toolchain_available() is None:
        print(
            "no Node or Bun on this machine. Reflex compiles a Vite frontend "
            "and cannot do it without one.\n"
            "This is a requirement of the machine that BUILDS the bundle, not "
            "of the machine that runs it -- the target serves the compiled "
            "output and never needs a JS toolchain.",
            file=sys.stderr,
        )
        return 0 if keep_going else 1

    print("compiling the Reflex frontend (this needs a minute)")
    result = subprocess.run(
        [sys.executable, "-m", "reflex", "export", "--frontend-only",
         "--no-zip"],
        cwd=RXAPP,
    )
    if result.returncode != 0:
        # Older Reflex has no --no-zip; the zip is harmless, the build is what
        # matters, so a second attempt without the flag is worth making before
        # giving up on the whole bundle.
        result = subprocess.run(
            [sys.executable, "-m", "reflex", "export", "--frontend-only"],
            cwd=RXAPP,
        )
    if result.returncode != 0:
        print("the Reflex frontend did not compile", file=sys.stderr)
        return 0 if keep_going else 1

    source = next((d for d in BUILD_DIRS if d.is_dir()), None)
    if source is None:
        print(
            "Reflex reported success but wrote no build directory; looked in "
            + ", ".join(str(d) for d in BUILD_DIRS),
            file=sys.stderr,
        )
        return 0 if keep_going else 1

    if TARGET.exists():
        shutil.rmtree(TARGET)
    # The gzipped twins Reflex emits beside every asset are for a server that
    # serves them directly. Ours does not, and they double the size of the one
    # part of this that has to stay small.
    shutil.copytree(source, TARGET, ignore=shutil.ignore_patterns("*.gz"))

    files = sum(1 for p in TARGET.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in TARGET.rglob("*") if p.is_file())
    print(f"{TARGET.relative_to(REPO)}  {files} files, {size / 1e6:.2f} MB")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.build_reflex_frontend")
    parser.add_argument(
        "--keep-going", action="store_true",
        help="warn instead of failing when the frontend cannot be built, so a "
             "bundle can still be cut on a machine with no JS toolchain",
    )
    args = parser.parse_args(argv)
    return build(keep_going=args.keep_going)


if __name__ == "__main__":
    raise SystemExit(main())
