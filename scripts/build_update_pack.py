"""Build a small update for a bundle that is already installed.

The full offline bundle is 150 MB, and almost all of it is things that do not
change: the PostgreSQL binaries, polars, scipy, pyarrow. A change to this
project changes one wheel of about a quarter of a megabyte. Re-cutting the
whole bundle into seven parts and asking for the whole transfer again to
deliver that is a cost with nothing on the other side of it.

    python -m scripts.build_update_pack

Produces ``dist/cmdm-update-<version>.zip``: the rebuilt ``cmdm`` wheel, the
source and tests that go with it, the verifier, and an ``update`` script that
reinstalls the wheel from disk with ``--no-index``. No internet on either end.

Deliberately *not* platform-specific. The cmdm wheel is pure Python -- the
compiled dependencies are the ones that need a platform tag, and none of them
are in here -- so one update pack works on every bundle, whatever it was built
for. That is only true while the dependency closure is unchanged; if a release
adds a dependency, it needs the full bundle, and this script says so rather
than producing an update that installs and then fails on an import.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent

#: What an update replaces. The wheel is the software; the rest is what the
#: bundle carries alongside it so the target can read and verify what it runs.
#:
PAYLOAD = ["src", "tests", "docs", "scripts", "README.md"]

#: Named individually rather than by copying ``data/``. The sample extract is
#: shipped content -- a release that adds a source column ships a sample
#: carrying it, and leaving the old file behind makes ``verify`` fail on a
#: bundle that is otherwise correct. But a developer's ``data/`` also collects
#: benchmark inputs, and copying the directory wholesale put a 100 MB file in a
#: pack whose entire reason for existing is that it is small.
DATA_FILES = ["life_admin_sample.csv"]

#: Taken from offline/. The updater and the scripts that launch it, plus the
#: bundle-root files a release can change. ``worker`` is in the list because a
#: release can add a subcommand -- this one adds ``rebuild`` -- and the launcher
#: has to be able to pass it through.
SCRIPTS = ["update.py", "update.cmd", "update.sh", "verify.py",
           "worker.cmd", "worker.sh", "OFFLINE-INSTALL.md"]


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}")


def _version() -> str:
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return document["project"]["version"]


def _declared_dependencies() -> set[str]:
    """Distribution names this release needs *in a bundle*.

    Read from the same TOP_LEVEL list the bundle builder installs from, not
    from every extra in pyproject: the dev extras and the optional ONNX runtime
    are deliberately not in a bundle, and listing them here would make every
    update refuse itself for packages the target was never meant to have.

    Compared on the target against what its wheelhouse holds, so an update that
    would leave a machine unable to import its own code is refused while there
    is still something to be done about it.
    """
    from scripts.build_offline_bundle import TOP_LEVEL, _requirements

    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(document["project"].get("dependencies", []))
    for spec in TOP_LEVEL:
        specs.extend(_requirements(spec))

    names = set()
    for spec in specs:
        name = spec.split(";")[0]
        for terminator in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(terminator)[0]
        if name:
            names.add(name.strip().lower().replace("_", "-"))
    return names


def build(out_dir: pathlib.Path) -> pathlib.Path:
    version = _version()
    staging = out_dir / f"cmdm-update-{version}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    print("building the cmdm wheel")
    wheels = staging / "wheels"
    wheels.mkdir()
    run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheels)])

    for name in PAYLOAD:
        source = REPO / name
        target = staging / name
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copy2(source, target)

    data = staging / "data"
    data.mkdir()
    for name in DATA_FILES:
        source = REPO / "data" / name
        if source.exists():
            shutil.copy2(source, data / name)

    for name in SCRIPTS:
        source = REPO / "offline" / name
        if source.exists():
            shutil.copy2(source, staging / name)

    (staging / "UPDATE.md").write_text(_update_md(version), encoding="utf-8")

    (staging / "DEPENDENCIES.txt").write_text(
        "\n".join(sorted(_declared_dependencies())) + "\n", encoding="utf-8"
    )

    rows = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{digest}  {path.relative_to(staging).as_posix()}")
    (staging / "MANIFEST.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")

    archive = out_dir / f"{staging.name}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            arcname = pathlib.Path(f"cmdm-update-{version}") / path.relative_to(staging)
            info = zipfile.ZipInfo.from_file(path, arcname.as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.suffix == ".sh":
                info.external_attr = (0o755 << 16) | (info.external_attr & 0xFFFF)
            z.writestr(info, path.read_bytes())

    size = archive.stat().st_size
    print(f"{archive}  {size / 1e6:.1f} MB, {len(rows)} files")
    return archive


# ---------------------------------------------------------------------------
# What ships inside the pack
# ---------------------------------------------------------------------------


def _update_md(version: str) -> str:
    return f"""# Deploying {version} to an installed bundle

You do not need the 150 MB bundle again. Nothing in it changed except this
project's own wheel: the PostgreSQL binaries, polars, scipy and the rest are the
same files you already have.

## Run it

Windows:

```
update.cmd  C:\\path\\to\\cmdm-offline
```

Linux / macOS:

```
./update.sh  /path/to/cmdm-offline
```

Give it the folder that contains `start.cmd` and `.venv`. With no argument it
updates the bundle in the parent folder, which is right if you unzipped this
pack inside it. Add `--check` to see what would happen and change nothing.

## What it does

1. **Checks** the target is an installed bundle and that this release needs no
   package the bundle lacks. Nothing is written until that passes.
2. **Backs up** everything it is about to replace into `backup-<timestamp>/`.
3. **Replaces** `src/`, `tests/`, `docs/`, `data/` and the root scripts, then
   installs the wheel with `--no-index` -- nothing is fetched.
4. **Migrates** the schema. A store one release behind otherwise fails on the
   first query rather than at start-up, which is a far worse place to find out.
5. **Checks identity**: does the stored crosswalk still mean what the installed
   mappings say it means? A release that changes a key kind changes what a
   stored identity *is*, and nothing in the database is violated by that -- so
   nothing else would ever report it.
6. **Reports** the installed version and the size of the store, so the run ends
   on evidence rather than on the absence of an error.

It never touches `pgdata`, `pgpassword` or your config files, and it never
deletes golden records.

## Then

```
verify.cmd --quick        prove the machine still runs it
worker.cmd backfill       bring the existing golden store forward
start.cmd                 http://127.0.0.1:8000/console/entities
```

`backfill` re-runs every batch already in the landing zone through the current
pipeline. That is how a release which computes something the last one did not
fills in the gap, without you re-uploading a file. It is idempotent: rows
already correct stay on the version they are on.

## If the updater reports RE-KEYED

This release changed a key kind, so parties in your store are keyed on
identities nothing writes to any more. **Do not backfill** -- it would mint a
second golden party for each rather than correct the first, and the updater
refuses to for that reason. Rebuild instead:

```
worker.cmd rebuild
```

That discards the derived store -- golden records, crosswalks, provenance, the
match ledger -- and recomputes all of it from the delivered bytes in the landing
zone, which are immutable and still there. It is a recomputation, not a data
loss. Two things do not survive: published person and policy ids change, and
steward decisions keyed on the old identities do not carry over. Run
`worker.cmd check` first if you want to see the finding on its own.
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.build_update_pack")
    parser.add_argument("--out", default="dist", help="where to write the pack")
    args = parser.parse_args(argv)

    out_dir = (REPO / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    build(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
