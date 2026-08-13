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
PAYLOAD = ["src", "tests", "docs", "README.md"]

#: Taken from offline/. The updater and the scripts that launch it, plus the
#: bundle-root files a release can change.
SCRIPTS = ["update.py", "update.cmd", "update.sh", "verify.py", "OFFLINE-INSTALL.md"]


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
    return f"""# Updating an installed bundle to {version}

You do not need the 150 MB bundle again. Nothing in it changed except this
project's own wheel: the PostgreSQL binaries, polars, scipy and the rest are
the same files you already have.

## Windows

```
update.cmd  C:\\path\\to\\cmdm-offline
```

## Linux / macOS

```
./update.sh  /path/to/cmdm-offline
```

Give it the folder that contains `start.cmd` and `.venv`. With no argument it
updates the bundle in the parent folder, which is right if you unzipped this
pack inside it.

The updater:

1. checks the bundle is the thing it says it is, and that this update needs no
   dependency the bundle does not already have;
2. reinstalls the `cmdm` wheel from disk with `--no-index`, so nothing is
   fetched;
3. replaces `src/`, `tests/`, `docs/` and `verify.py`, keeping a copy of what
   it replaced in `backup-<timestamp>/` beside the bundle;
4. leaves your database, your API keys and your `config.cmd` untouched.

## Then

```
verify.cmd --quick            prove the machine still runs it
worker.cmd backfill           bring the existing golden store forward
start.cmd                     http://127.0.0.1:8000/console/entities
```

`backfill` re-runs every batch already in the landing zone through the current
pipeline. It is what fills in anything this release computes that the previous
one did not, without re-uploading a file. It is idempotent: an unchanged record
stays one version, and running it twice does nothing the second time.
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
