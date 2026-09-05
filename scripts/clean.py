"""Remove everything that can be rebuilt, and nothing that cannot.

    python -m scripts.clean --dry-run     say what would go
    python -m scripts.clean               do it

This project's source is about 1.5 MB. A working copy of it reached 612 MB,
essentially all of it build output nobody had a reason to keep: bundle staging
trees left beside the zips built from them, every update pack ever cut, a 98 MB
benchmark extract generated once for a measurement, and the usual caches.

The rule this script encodes is the one worth remembering: **everything it
deletes is either ignored by git or reproducible by a documented command.**
Nothing here reads the working tree to decide -- the list is explicit, so a
future directory does not get swept up because it happened to match a pattern.

What it deliberately does *not* touch:

* ``data/life_admin_sample.csv``. It is git-ignored and regenerable, but the
  test suite reads it, and deleting it turns a clean checkout into one where
  half the suite fails for a reason that is not obvious. Use ``--samples`` to
  remove it anyway, and ``python -m scripts.generate_sample_data`` to bring it
  back.
* Anything tracked by git.
* ``pgdata/`` -- that is a database, not a build artifact. If it is a scratch
  store, delete it deliberately.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
from collections.abc import Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent

#: (path, what it is, how to get it back). The third column is the reason each
#: of these is safe: an entry with no way back does not belong in this list.
TARGETS: list[tuple[str, str, str]] = [
    ("dist", "built bundles and update packs",
     "python -m scripts.build_offline_bundle / build_update_pack / "
     "build_sas2csv_bundle"),
    ("data/bench.csv", "benchmark extract, generated for one measurement",
     "python -m scripts.generate_sample_data --rows N --out data/bench.csv"),
    (".pytest_cache", "pytest cache", "rebuilt on the next run"),
    (".ruff_cache", "ruff cache", "rebuilt on the next run"),
    (".mypy_cache", "mypy cache", "rebuilt on the next run"),
    ("build", "setuptools/hatch build tree", "rebuilt on the next build"),
    ("htmlcov", "coverage HTML report", "pytest --cov"),
    (".coverage", "coverage data", "pytest --cov"),
]

#: Removed wherever they appear. Bytecode and egg metadata are regenerated on
#: import and on install respectively.
SWEEP = ["__pycache__", "*.egg-info"]

#: Only with --samples. Regenerable, but the suite needs it.
SAMPLES = ["data/life_admin_sample.csv"]


def _size(path: pathlib.Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _human(total: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} GB"


def clean(*, dry_run: bool = False, samples: bool = False) -> int:
    found: list[tuple[pathlib.Path, int, str]] = []

    for name, what, _ in TARGETS:
        path = REPO / name
        if path.exists():
            found.append((path, _size(path), what))

    for pattern in SWEEP:
        for path in sorted(REPO.rglob(pattern)):
            # A build artifact inside dist/ is already counted by dist/ itself.
            if any(parent.name == "dist" for parent in path.parents):
                continue
            if path.exists():
                found.append((path, _size(path), pattern))

    if samples:
        for name in SAMPLES:
            path = REPO / name
            if path.exists():
                found.append((path, _size(path), "sample extract"))

    if not found:
        print("nothing to remove; this tree is already clean")
        return 0

    total = 0
    for path, size, what in found:
        total += size
        action = "would remove" if dry_run else "removed"
        print(f"  {action:<13} {_human(size):>9}  "
              f"{path.relative_to(REPO)}  ({what})")
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    verb = "would free" if dry_run else "freed"
    print(f"\n{verb} {_human(total)} across {len(found)} paths")
    if not samples and (REPO / "data" / "life_admin_sample.csv").exists():
        print("kept data/life_admin_sample.csv — the test suite reads it "
              "(--samples removes it too)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.clean")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be removed and change nothing")
    parser.add_argument(
        "--samples", action="store_true",
        help="also remove the generated sample extract the tests read",
    )
    args = parser.parse_args(argv)
    return clean(dry_run=args.dry_run, samples=args.samples)


if __name__ == "__main__":
    raise SystemExit(main())
