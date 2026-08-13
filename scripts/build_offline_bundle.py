"""Build the offline bundle: wheels, source, scripts, checksums, zip.

The bundle is per platform and per Python version because the compiled parts --
polars, scipy, psycopg, pyarrow, and the embedded PostgreSQL -- are. There is no
universal build, and pretending otherwise produces a zip that fails on arrival
with "no matching distribution", on a machine with no internet to diagnose it
from.

    python -m scripts.build_offline_bundle --platform win_amd64 --python 3.11

Requires internet, on the machine that builds it. That is the point: it is run
once here so that the target never needs any.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The runtime closure. Kept in step with pyproject's extras rather than
#: duplicated from them -- these are the top-level names, and pip resolves the
#: rest -- plus what the test suite needs, because a bundle you cannot verify on
#: arrival is a bundle you have to trust.
TOP_LEVEL = ["cmdm[vector,store,api]", "pytest", "httpx", "pglast"]

#: PostgreSQL itself, shipped as plain files rather than as a wheel.
#:
#: pgserver publishes the same binaries as a Python package, but only for
#: CPython 3.9-3.12 -- so depending on it would cap the whole system at 3.12 on
#: account of a database engine that does not care what interpreter is running.
#: The binaries are extracted from whichever wheel exists and installed as
#: `pgsql/`, where cmdm.embedded looks for them. On Windows every DLL lives
#: inside that tree, so it is self-contained.
PGSERVER_VERSION = "0.1.4"
PGSERVER_BUILD_PYTHON = "3.12"

#: Copied verbatim into the bundle. The source tree ships alongside the wheel so
#: the tests can run on the target and an operator can read what they are about
#: to run.
PAYLOAD = ["src", "tests", "scripts", "docs", "pyproject.toml", "README.md"]


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}")


def build(platform: str, python: str, out_dir: pathlib.Path) -> pathlib.Path:
    staging = out_dir / f"cmdm-offline-{platform}-py{python.replace('.', '')}"
    if staging.exists():
        shutil.rmtree(staging)
    wheels = staging / "wheels"
    wheels.mkdir(parents=True)

    abi = f"cp{python.replace('.', '')}"
    print(f"downloading wheels for {platform} / cp{python}")
    run([
        sys.executable, "-m", "pip", "download", "--dest", str(wheels),
        "--only-binary=:all:", "--platform", platform,
        "--python-version", python, "--implementation", "cp", "--abi", abi,
        *(dep for spec in TOP_LEVEL for dep in _requirements(spec)),
    ])

    _postgres_binaries(staging, platform, python)

    print("building the cmdm wheel")
    run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheels)])

    for name in PAYLOAD:
        source = REPO / name
        target = staging / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)

    data = staging / "data"
    data.mkdir(exist_ok=True)
    shutil.copy2(REPO / "data" / "life_admin_sample.csv", data)

    for script in sorted((REPO / "offline").iterdir()):
        shutil.copy2(script, staging / script.name)

    _checksums(staging)
    return _zip(staging, out_dir)


def _postgres_binaries(staging: pathlib.Path, platform: str, python: str) -> None:
    """Unpack a PostgreSQL installation into the bundle as ``pgsql/``."""
    import tempfile

    print("fetching PostgreSQL binaries")
    with tempfile.TemporaryDirectory() as scratch:
        # The binaries are native and identical across the wheel's Python tags,
        # so the wheel is fetched for whatever tag exists rather than for the
        # target interpreter -- which may have no pgserver wheel at all.
        build_python = python if _pgserver_supports(python) else PGSERVER_BUILD_PYTHON
        abi = f"cp{build_python.replace('.', '')}"
        run([
            sys.executable, "-m", "pip", "download", "--dest", scratch, "--no-deps",
            "--only-binary=:all:", "--platform", platform,
            "--python-version", build_python, "--implementation", "cp", "--abi", abi,
            f"pgserver=={PGSERVER_VERSION}",
        ])
        wheel = next(pathlib.Path(scratch).glob("pgserver-*.whl"))
        target = staging / "pgsql"
        with zipfile.ZipFile(wheel) as z:
            members = [n for n in z.namelist() if n.startswith("pgserver/pginstall/")]
            for name in members:
                relative = name[len("pgserver/pginstall/"):]
                if not relative:
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if name.endswith("/"):
                    continue
                destination.write_bytes(z.read(name))
                # initdb and pg_ctl have to be runnable; zip carries no mode.
                if "/bin/" in name or destination.suffix in ("", ".sh"):
                    destination.chmod(0o755)
        count = sum(1 for _ in target.rglob("*"))
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"  pgsql/ {count} files, {size / 1e6:.0f} MB")


def _pgserver_supports(python: str) -> bool:
    major, minor = (int(part) for part in python.split(".")[:2])
    return (major, minor) <= (3, 12)


def _requirements(spec: str) -> list[str]:
    """Expand a top-level spec into what pip download needs.

    ``cmdm[...]`` cannot be downloaded from an index -- it is this project --
    so its extras are expanded into the underlying requirements by reading the
    metadata rather than by restating them here, where they would drift.
    """
    if not spec.startswith("cmdm"):
        return [spec]

    import tomllib

    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = spec[spec.index("[") + 1 : spec.index("]")].split(",")
    optional = document["project"]["optional-dependencies"]
    out: list[str] = []
    for extra in extras:
        out.extend(optional[extra.strip()])
    return out


def _checksums(staging: pathlib.Path) -> None:
    rows = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{digest}  {path.relative_to(staging).as_posix()}")
    (staging / "MANIFEST.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"checksummed {len(rows)} files")


def split(archive: pathlib.Path, chunk_mib: int = 24) -> list[pathlib.Path]:
    """Cut the archive into transferable parts, beside it.

    Most transfer paths cap an attachment well below the size of a bundle that
    carries a database engine, so the split is part of building it rather than
    something to improvise later. ``offline/rejoin.py`` puts it back and checks
    the result against MANIFEST-level SHA-256.
    """
    parts_dir = archive.parent / "parts"
    parts_dir.mkdir(exist_ok=True)
    for stale in parts_dir.glob(f"{archive.name}.*"):
        stale.unlink()

    data = archive.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    chunk = chunk_mib * 1024 * 1024
    written = []
    for index in range(0, len(data), chunk):
        part = parts_dir / f"{archive.name}.{index // chunk + 1:03d}"
        part.write_bytes(data[index:index + chunk])
        written.append(part)

    (parts_dir / "SHA256.txt").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    shutil.copy2(REPO / "offline" / "rejoin.py", parts_dir / "rejoin.py")
    print(f"split into {len(written)} parts of <= {chunk_mib} MiB in {parts_dir}")
    return written


def _zip(staging: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    archive = out_dir / f"{staging.name}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            arcname = pathlib.Path("cmdm-offline") / path.relative_to(staging)
            info = zipfile.ZipInfo.from_file(path, arcname.as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.suffix == ".sh":
                # Zip does not carry the execute bit unless it is written in.
                # Irrelevant on Windows, and exactly what makes "./verify.sh"
                # fail with Permission denied everywhere else -- which reads as
                # a broken bundle rather than a missing chmod.
                info.external_attr = (0o755 << 16) | (info.external_attr & 0xFFFF)
            z.writestr(info, path.read_bytes())
    print(f"{archive}  {archive.stat().st_size / 1e6:.1f} MB")
    return archive


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.build_offline_bundle")
    parser.add_argument(
        "--platform", default="win_amd64",
        help="pip platform tag: win_amd64, manylinux2014_x86_64, macosx_11_0_arm64",
    )
    parser.add_argument("--python", default="3.11", help="target Python, e.g. 3.11")
    parser.add_argument("--out", default="dist", help="where to write the bundle")
    parser.add_argument(
        "--split", type=int, metavar="MIB", default=0,
        help="also cut the zip into parts of at most MIB megabytes",
    )
    args = parser.parse_args(argv)

    out_dir = (REPO / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = build(args.platform, args.python, out_dir)
    if args.split:
        split(archive, chunk_mib=args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
