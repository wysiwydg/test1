"""Build the offline bundle for the SAS to CSV converter.

    python -m scripts.build_sas2csv_bundle --platform win_amd64 --python 3.13

Requires internet, on the machine that builds it. That is the point: it is run
once here so that the machine holding the extracts never needs any.

Separate from ``build_offline_bundle`` on purpose. That one carries the whole
MDM system -- Arrow, polars, psycopg, PostgreSQL itself, about 150 MB -- because
that is what the system needs to run. The converter needs pandas and numpy and
nothing else, it runs on whichever machine the SAS extracts land on rather than
on the MDM host, and a 25 MB zip is something an operator can be sent. The
dependency *declaration* is shared, in pyproject's ``sas`` extra; only the
packaging is separate.

What it does share is the part that is easy to get wrong. ``pip download
--platform win_amd64`` chooses wheel *tags* for the target but evaluates
environment *markers* against the machine downloading, so a Windows-only
dependency is silently skipped when building on Linux -- for pandas that is
tzdata, and a bundle missing it installs and then fails on the first timestamp.
The fixpoint loop that catches it lives in ``build_offline_bundle`` and is
imported rather than reimplemented, because two copies of that logic would
eventually disagree and only one of them would be tested.
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

from scripts.build_offline_bundle import _marker_environment, _missing_for_target

REPO = pathlib.Path(__file__).resolve().parent.parent

#: What the converter imports. Kept in step with pyproject's ``sas`` extra --
#: pip resolves the rest of the closure from these two.
TOP_LEVEL = ["pandas>=2.2", "numpy>=1.26"]

#: Not needed to convert anything.
#:
#: pyreadstat is the ``--reader fast`` path, which trades the no-extraction
#: property for throughput; without the wheel that flag refuses instead of
#: failing obscurely. pytest is here because a bundle that cannot be verified on
#: arrival is a bundle that has to be trusted, and the test suite generates its
#: own sas7bdat files, so it needs neither SAS nor a network to mean something.
OPTIONAL = ["pyreadstat>=1.2.8", "pytest>=8.0"]

#: The converter, its tests, and the launchers. Laid out exactly as the
#: repository lays them out, so the commands in the documentation are the same
#: commands on the target.
PAYLOAD = [
    ("scripts/sas2csv.py", "scripts/sas2csv.py"),
    ("tests/test_sas2csv.py", "tests/test_sas2csv.py"),
    ("tests/sas7bdat_fixtures.py", "tests/sas7bdat_fixtures.py"),
    ("docs/06-sas7bdat-to-postgres.md", "docs/06-sas7bdat-to-postgres.md"),
]


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}")


def _download(wheels: pathlib.Path, platform: str, python: str) -> None:
    abi = f"cp{python.replace('.', '')}"
    print(f"downloading wheels for {platform} / cp{python}")
    run([
        sys.executable, "-m", "pip", "download", "--dest", str(wheels),
        "--only-binary=:all:", "--platform", platform,
        "--python-version", python, "--implementation", "cp", "--abi", abi,
        *TOP_LEVEL,
    ])
    print(f"downloading the optional wheels ({', '.join(OPTIONAL)})")
    run([
        sys.executable, "-m", "pip", "download", "--dest", str(wheels),
        "--only-binary=:all:", "--platform", platform,
        "--python-version", python, "--implementation", "cp", "--abi", abi,
        *OPTIONAL,
    ])


def _complete_closure(wheels: pathlib.Path, platform: str, python: str) -> None:
    """Fetch what the target needs and this machine's markers hid.

    Iterated to a fixpoint: a package pulled in this way brings its own
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
        "dependency closure did not settle: still missing "
        f"{sorted(_missing_for_target(wheels, environment))}"
    )


def _versions(wheels: pathlib.Path) -> dict[str, str]:
    found = {}
    for wheel in sorted(wheels.glob("*.whl")):
        name, version = wheel.name.split("-")[:2]
        found[name.replace("_", "-").lower()] = version
    return found


def _readme(platform: str, python: str, versions: dict[str, str]) -> str:
    listed = "\n".join(f"* {name} {version}" for name, version in sorted(versions.items()))
    windows = platform.startswith("win")
    install = "install.cmd" if windows else "./install.sh"
    convert = "convert.cmd" if windows else "./convert.sh"
    verify = "verify.cmd" if windows else "./verify.sh"
    example = (
        r"convert.cmd C:\extracts\POLICY.zip --out C:\staging"
        if windows
        else "./convert.sh /extracts/POLICY.zip --out /staging"
    )
    return f"""# SAS7BDAT to CSV, offline

Converts zipped sas7bdat extracts into CSV that Azure Database for PostgreSQL
can `COPY`, on a machine with no internet. Built for **{platform} / Python
{python}**; the compiled wheels will be refused under any other Python version.

## Install

    {install}

Creates `.venv` here, installs the wheels below into it, and then proves the
result works by importing pandas and running the converter's help. Nothing is
downloaded.

## Check it before trusting it

    {verify}

Generates sas7bdat files on this machine and converts them, asserting the things
that actually go wrong: whole numbers written as `1.0` that will not load into
`bigint`, 31DEC9999 sentinel dates, NUL bytes inside char fields, an encoding
pandas has never heard of. Needs no SAS and no network.

## Convert

    {example}

Writes one CSV, one `CREATE TABLE`, and a `load.sql` per run. Nothing is
extracted from the zip -- the sas7bdat is read as a stream -- so no scratch
space is needed beyond the CSV itself. `{convert} --help` lists every option;
`--list` prints an archive's columns and inferred types without converting.

## Load

    psql "host=SERVER.postgres.database.azure.com port=5432 dbname=DB \\
          user=USER sslmode=require" -v ON_ERROR_STOP=1 -f load.sql

`\\copy` streams from this machine, so the CSVs do not have to be anywhere the
server can see.

## What is in here

{listed}

`docs/06-sas7bdat-to-postgres.md` documents the type mapping, the CSV dialect
and every decision the converter makes on your behalf.
"""


def _checksums(staging: pathlib.Path) -> None:
    lines = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(staging).as_posix()}")
    (staging / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _zip(staging: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    archive = out_dir / f"{staging.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                handle.write(path, staging.name / path.relative_to(staging))
    return archive


def build(platform: str, python: str, out_dir: pathlib.Path, *,
          keep_staging: bool = False) -> pathlib.Path:
    staging = out_dir / f"sas2csv-offline-{platform}-py{python.replace('.', '')}"
    if staging.exists():
        shutil.rmtree(staging)
    wheels = staging / "wheels"
    wheels.mkdir(parents=True)

    _download(wheels, platform, python)
    _complete_closure(wheels, platform, python)

    for source, target in PAYLOAD:
        destination = staging / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / source, destination)
    # scripts/ is imported as a package on the target, exactly as it is here.
    (staging / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    # pytest finds the converter through this, and nothing else needs it.
    (staging / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\ntestpaths = tests\naddopts = -q\n", encoding="utf-8"
    )

    for script in sorted((REPO / "offline" / "sas2csv").iterdir()):
        if script.is_file():
            shutil.copy2(script, staging / script.name)

    versions = _versions(wheels)
    (staging / "README.md").write_text(_readme(platform, python, versions), encoding="utf-8")
    _checksums(staging)

    archive = _zip(staging, out_dir)
    if not keep_staging:
        shutil.rmtree(staging)
    return archive


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.build_sas2csv_bundle",
        description="Build the offline SAS to CSV converter bundle.",
    )
    parser.add_argument("--platform", default="win_amd64",
                        help="target wheel platform tag (default: win_amd64)")
    parser.add_argument("--python", default="3.13",
                        help="target Python version (default: 3.13)")
    parser.add_argument("--out", default="dist", help="where to write the bundle")
    parser.add_argument("--keep-staging", action="store_true",
                        help="leave the unpacked tree beside the zip")
    arguments = parser.parse_args(argv)

    out_dir = REPO / arguments.out
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = build(
        arguments.platform, arguments.python, out_dir,
        keep_staging=arguments.keep_staging,
    )
    size = archive.stat().st_size / 1e6
    print(f"\nwrote {archive.relative_to(REPO)} ({size:,.1f} MB)")
    print("Copy it to the target, unzip it, and run install.cmd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
