"""Install the SAS to CSV converter from the wheels in this folder.

    python install.py

No network. Standard library only -- an installer that needs a package to run
is no use on the machine this is for. Creates ``.venv`` beside itself, installs
every wheel from ``wheels/`` into it, and then proves the result works by
importing pandas and asking the converter for its own help text. If either of
those fails, the install failed, and it says so here rather than in three weeks
when somebody tries to convert an extract.
"""

from __future__ import annotations

import pathlib
import platform
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
WHEELS = HERE / "wheels"
VENV = HERE / ".venv"

TOP_LEVEL = ["pandas", "numpy"]
OPTIONAL = ["pyreadstat", "pytest"]


def built_for() -> str | None:
    """The Python version this wheelhouse was built for, read off the wheels.

    Compiled wheels carry their ABI in the filename -- ``cp313`` -- and a bundle
    built for one interpreter and installed under another fails with "no
    matching distribution", which is a confusing way to learn that. Derived
    rather than written down, because a constant in this file and the wheels
    beside it would eventually disagree, and the wheels are the truth.
    """
    for wheel in sorted(WHEELS.glob("*.whl")):
        for tag in wheel.stem.split("-"):
            if tag.startswith("cp3") and tag[2:].isdigit():
                return f"{tag[2]}.{tag[3:]}"
    return None


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def fail(message: str) -> int:
    print(f"\nSTOPPED: {message}\n", file=sys.stderr)
    return 1


def venv_python() -> pathlib.Path:
    if platform.system() == "Windows":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(argv: list[str], what: str) -> None:
    result = subprocess.run(argv, cwd=HERE)
    if result.returncode != 0:
        raise SystemExit(fail(f"{what} failed"))


def present(name: str) -> bool:
    return any(WHEELS.glob(f"{name.replace('-', '_')}-*.whl")) or any(
        WHEELS.glob(f"{name}-*.whl")
    )


def main() -> int:
    if not WHEELS.is_dir() or not any(WHEELS.glob("*.whl")):
        return fail(f"no wheels in {WHEELS}. This bundle is incomplete.")

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    target = built_for()
    if target and target != running:
        say(f"warning: these wheels were built for Python {target}, and this is "
            f"{running}. They will very likely be refused.")

    if not VENV.exists():
        say(f"creating a virtual environment in {VENV}")
        run([sys.executable, "-m", "venv", str(VENV)], "creating the virtual environment")
    else:
        say(f"using the existing virtual environment in {VENV}")

    wanted = list(TOP_LEVEL) + [name for name in OPTIONAL if present(name)]
    say(f"installing {', '.join(wanted)} from {WHEELS.name}/ (no network)")
    run(
        [
            str(venv_python()), "-m", "pip", "install",
            "--no-index", "--find-links", str(WHEELS),
            "--disable-pip-version-check", "--no-input", "--quiet",
            *wanted,
        ],
        "installing the wheels",
    )

    say("checking that it actually runs")
    run(
        [
            str(venv_python()), "-c",
            "import pandas, numpy; print('  pandas', pandas.__version__, "
            "'numpy', numpy.__version__)",
        ],
        "importing pandas",
    )
    run(
        [str(venv_python()), "-m", "scripts.sas2csv", "--help"],
        "running the converter",
    )

    print()
    say("installed. Convert an extract with:")
    say(r"    convert.cmd C:\path\to\EXTRACT.zip --out C:\path\to\staging"
        if platform.system() == "Windows"
        else "    ./convert.sh /path/to/EXTRACT.zip --out /path/to/staging")
    if present("pytest"):
        say("or check it against generated sas7bdat files first:")
        say("    verify.cmd" if platform.system() == "Windows" else "    ./verify.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
