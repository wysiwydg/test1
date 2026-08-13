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
TOP_LEVEL = ["cmdm[vector,store,api,embedded]", "pytest", "httpx", "pglast"]

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
    args = parser.parse_args(argv)

    out_dir = (REPO / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    build(args.platform, args.python, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
