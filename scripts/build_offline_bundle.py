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
import re
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
#:
#: `ai` carries onnxruntime, which nothing imports until a model is promoted in
#: the registry. It is here anyway because an update pack ships Python files and
#: cannot install a compiled dependency on a machine with no internet: leaving it
#: out would mean a target that can never run an ONNX model, no matter what is
#: approved for it later. About 15 MB for an option kept open.
TOP_LEVEL = ["cmdm[vector,store,api]", "pytest", "httpx", "pglast"]

#: In the bundle, but not required to run it.
#:
#: onnxruntime is imported only when a model is ACTIVE in the registry; with
#: nothing promoted both AI paths run their reference implementations and never
#: touch it. It is in a bundle anyway because an update pack ships Python files
#: and cannot install a compiled dependency on a machine with no internet:
#: leaving it out would mean a target that can never run an ONNX model, whatever
#: is approved for it later. About 14 MB for an option kept open.
#:
#: Downloaded separately from TOP_LEVEL for two reasons. It must not be in the
#: required-dependency list an update pack checks against the target, or every
#: bundle cut before this line existed would be refused an update over a package
#: its code never imports. And it needs a wider set of platform tags than the
#: rest -- see EXTRA_PLATFORM_TAGS.
OPTIONAL = ["onnxruntime>=1.18"]

#: Names in OPTIONAL, for the update pack to subtract. Derived rather than
#: written twice, because two lists that must agree eventually will not.
OPTIONAL_AT_RUNTIME = {
    re.split(r"[<>=!~\[ ]", spec)[0].strip().lower() for spec in OPTIONAL
}

#: Additional wheel tags accepted for OPTIONAL packages only.
#:
#: onnxruntime stopped publishing manylinux2014 wheels after 1.16: current
#: releases are tagged manylinux_2_27, so a bundle asking only for manylinux2014
#: resolves nothing newer and the build fails outright. Widening the tag set for
#: every package would instead raise the glibc floor of the whole bundle to
#: satisfy one optional dependency, which is the wrong trade in the other
#: direction -- so the widening is confined to the package that needs it.
EXTRA_PLATFORM_TAGS = {
    "manylinux2014_x86_64": ["manylinux_2_27_x86_64", "manylinux_2_28_x86_64"],
    "manylinux2014_aarch64": ["manylinux_2_27_aarch64", "manylinux_2_28_aarch64"],
}

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

#: The updater belongs to an update pack, not to a bundle, and is deliberately
#: left out of one.
#:
#: A bundle that carried it would carry the copy that existed on the day it was
#: built, and an update pack cannot replace it: the updater excludes itself from
#: its own payload, because a script overwriting itself mid-run is not a thing
#: to arrange. So the bundle would keep a stale updater at its root forever, for
#: somebody to eventually run. Deployment is: unzip the pack, run the updater
#: *it* carries.
UPDATER = {"update.py", "update.cmd", "update.sh"}


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}")


def build(platform: str, python: str, out_dir: pathlib.Path, *,
          keep_staging: bool = False) -> pathlib.Path:
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

    _optional_wheels(wheels, platform, python)
    _complete_closure(wheels, platform, python)
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
        # Files only. A __pycache__ turns up there the moment anything imports
        # one of these scripts, and copying it as a file fails the whole build
        # on the last step before the zip.
        if script.is_file() and script.name not in UPDATER:
            shutil.copy2(script, staging / script.name)

    _checksums(staging)
    archive = _zip(staging, out_dir)

    # The staging tree is the zip, uncompressed and unpacked -- 181 MB sitting
    # beside the 148 MB archive built from it, useful only while the zip is
    # being written. Left behind, three or four builds fill a disk with copies
    # of files that already exist in the archive next to them.
    if not keep_staging:
        shutil.rmtree(staging)
    return archive


def _marker_environment(platform: str, python: str) -> dict[str, str]:
    """The PEP 508 environment of the *target*, not of this machine."""
    if platform.startswith("win"):
        system, sys_platform, os_name = "Windows", "win32", "nt"
        machine = "AMD64"
    elif platform.startswith("macosx"):
        system, sys_platform, os_name = "Darwin", "darwin", "posix"
        machine = "arm64" if "arm64" in platform else "x86_64"
    else:
        system, sys_platform, os_name = "Linux", "linux", "posix"
        machine = "aarch64" if "aarch64" in platform else "x86_64"

    return {
        "sys_platform": sys_platform,
        "platform_system": system,
        "os_name": os_name,
        "platform_machine": machine,
        "python_version": python,
        "python_full_version": f"{python}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "extra": "",
    }


def _declared_requirements(wheel: pathlib.Path) -> list[str]:
    import email

    with zipfile.ZipFile(wheel) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        message = email.message_from_bytes(z.read(name))
    return message.get_all("Requires-Dist") or []


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _present(wheels: pathlib.Path) -> set[str]:
    return {_canonical(w.name.split("-")[0]) for w in wheels.glob("*.whl")}


def _missing_for_target(wheels: pathlib.Path, environment: dict[str, str]) -> set[str]:
    """Requirements the target needs that this wheelhouse does not contain.

    ``pip download --platform`` chooses wheel *tags* for the target but still
    evaluates environment *markers* against the machine doing the downloading.
    So a dependency guarded by ``sys_platform == "win32"`` is silently skipped
    when building on Linux, and the omission only surfaces on the target --
    where there is no network to fix it with. On this project that was colorama
    (click, pytest) and tzdata, which psycopg needs on Windows to resolve the
    timezones every timestamptz column depends on.
    """
    from packaging.requirements import Requirement

    have = _present(wheels)
    missing: set[str] = set()
    for wheel in wheels.glob("*.whl"):
        for raw in _declared_requirements(wheel):
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            if _canonical(requirement.name) not in have:
                missing.add(str(requirement).split(";")[0].strip())
    return missing


def _optional_wheels(wheels: pathlib.Path, platform: str, python: str) -> None:
    """Fetch the packages a bundle carries but does not need to run.

    Separate from the main download so one optional package cannot dictate the
    wheel tags -- and therefore the minimum glibc -- of everything else in the
    bundle.

    Resolved into a scratch directory and then filtered, because the widened tag
    set applies to whatever pip pulls in transitively as well. onnxruntime needs
    only ``numpy>=1.21.6``, but offered a newer tag it fetches the newest numpy
    there is; two numpy wheels in one wheelhouse means ``pip install --no-index``
    silently takes the higher version, which is neither the one the required
    closure resolved nor one whose glibc floor anybody chose. Only packages the
    bundle does not already have are kept.
    """
    if not OPTIONAL:
        return

    import tempfile

    abi = f"cp{python.replace('.', '')}"
    tags = [platform, *EXTRA_PLATFORM_TAGS.get(platform, [])]
    print(f"downloading optional wheels ({', '.join(OPTIONAL)})")

    with tempfile.TemporaryDirectory() as scratch:
        staged = pathlib.Path(scratch)
        run([
            sys.executable, "-m", "pip", "download", "--dest", str(staged),
            "--only-binary=:all:",
            *(argument for tag in tags for argument in ("--platform", tag)),
            "--python-version", python, "--implementation", "cp", "--abi", abi,
            *OPTIONAL,
        ])

        have = _present(wheels)
        for wheel in sorted(staged.glob("*.whl")):
            name = _canonical(wheel.name.split("-")[0])
            if name in have:
                print(f"  keeping the resolved {name}, not the optional pass's "
                      f"{wheel.name}")
                continue
            shutil.copy2(wheel, wheels / wheel.name)


def _complete_closure(wheels: pathlib.Path, platform: str, python: str) -> None:
    """Fetch what the target needs and this machine's markers hid.

    Iterated to a fixpoint because a package pulled in this way brings its own
    requirements, which may themselves be platform-gated.
    """
    environment = _marker_environment(platform, python)
    abi = f"cp{python.replace('.', '')}"

    for _ in range(5):
        missing = _missing_for_target(wheels, environment)
        if not missing:
            return
        print(f"target needs {len(missing)} package(s) this platform's markers hid: "
              f"{', '.join(sorted(missing))}")
        run([
            sys.executable, "-m", "pip", "download", "--dest", str(wheels),
            "--no-deps", "--only-binary=:all:", "--platform", platform,
            "--python-version", python, "--implementation", "cp", "--abi", abi,
            *sorted(missing),
        ])

    raise SystemExit(
        f"dependency closure did not settle: still missing "
        f"{sorted(_missing_for_target(wheels, environment))}"
    )


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
    parser.add_argument(
        "--keep-staging", action="store_true",
        help="leave the unpacked tree beside the zip (for inspecting a build; "
             "it is a full second copy of the bundle)",
    )
    args = parser.parse_args(argv)

    out_dir = (REPO / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = build(args.platform, args.python, out_dir,
                    keep_staging=args.keep_staging)
    if args.split:
        split(archive, chunk_mib=args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
