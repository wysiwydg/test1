"""Install Customer MDM from the wheels in this folder. No network required.

Deliberately plain: standard library only, no third-party imports, and every
step announced. An installer that fails silently on an air-gapped machine is
worse than one that refuses loudly, because the person running it has no
internet to search the symptom with.

    python install.py

Creates a virtual environment in .venv, installs every wheel from wheels/ into
it, generates the identifier-hashing secret, and writes config.cmd for the
other scripts to read.
"""

from __future__ import annotations

import base64
import pathlib
import platform
import re
import secrets
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
WHEELS = HERE / "wheels"
VENV = HERE / ".venv"

#: Wheels are compiled per interpreter version, so a bundle is built for one.
#: The set here is what this build *could* be; BUILT_FOR below is what it is.
#: Installing under anything else fails with "no matching distribution", which
#: is a confusing way to learn the bundle was built for a different Python.
SUPPORTED = {(3, 11), (3, 12), (3, 13)}

TOP_LEVEL = ["cmdm[vector,store,api]", "pytest", "httpx", "pglast"]


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
        raise SystemExit(fail(f"{what} failed (exit {result.returncode})"))


#: pip's message for a package that is simply not in the wheelhouse.
MISSING_PATTERN = re.compile(
    r"No matching distribution found for ([A-Za-z0-9._-]+)", re.MULTILINE
)


def _explain_install_failure(stderr: str) -> int:
    """Turn pip's failure into something the person in front of it can act on.

    The failure that matters here has one cause and one fix, and pip's wording
    points at neither. A wheelhouse built on Linux for Windows silently omits
    anything guarded by a marker like ``sys_platform == "win32"``, because pip
    resolves markers against the machine doing the downloading, not the target.
    The result is a bundle that looks complete and fails on arrival -- with no
    network to fetch the missing piece from.
    """
    missing = sorted(set(MISSING_PATTERN.findall(stderr)))
    print()
    if missing:
        print(f"STOPPED: this bundle is missing {len(missing)} package(s): "
              f"{', '.join(missing)}", file=sys.stderr)
        print(
            "\nThat is a fault in the bundle, not in this machine, and it "
            "cannot be fixed here\nwithout a network. Ask for a rebuild that "
            "includes them.\n",
            file=sys.stderr,
        )
    else:
        print("STOPPED: the offline install failed; pip's output is above.",
              file=sys.stderr)
    return 1


def main() -> int:
    print("\nCustomer MDM — offline install\n")

    version = sys.version_info[:2]
    say(f"python {sys.version.split()[0]} on {platform.system()} {platform.machine()}")
    if version not in SUPPORTED:
        wanted = ", ".join(f"{a}.{b}" for a, b in sorted(SUPPORTED))
        return fail(
            f"this bundle contains wheels for Python {wanted} only, and you are "
            f"running {version[0]}.{version[1]}. The compiled parts (polars, "
            "scipy, psycopg, the embedded PostgreSQL) cannot be used across "
            "versions. Run this with a matching interpreter, or ask for a "
            "bundle rebuilt for yours."
        )

    if not WHEELS.is_dir() or not any(WHEELS.glob("*.whl")):
        return fail(f"no wheels found in {WHEELS}. Was the zip fully extracted?")
    say(f"{len(list(WHEELS.glob('*.whl')))} wheels found")

    # pip may be absent from a bare interpreter. ensurepip carries its own copy,
    # so this still needs no network.
    try:
        import pip  # noqa: F401
    except ImportError:
        say("pip missing; bootstrapping it from the standard library")
        run([sys.executable, "-m", "ensurepip", "--default-pip"], "ensurepip")

    if not venv_python().exists():
        say(f"creating virtual environment in {VENV.name}")
        run([sys.executable, "-m", "venv", str(VENV)], "creating the virtual environment")
    else:
        say(f"reusing existing virtual environment in {VENV.name}")

    say("installing (offline — pip is forbidden from reaching the network)")
    result = subprocess.run(
        [
            str(venv_python()), "-m", "pip", "install",
            "--no-index",                       # never consult PyPI
            "--find-links", str(WHEELS),        # resolve only from here
            "--disable-pip-version-check",
            "--no-warn-script-location",
            *TOP_LEVEL,
        ],
        cwd=HERE, capture_output=True, text=True,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        return _explain_install_failure(result.stderr)

    config = HERE / "config.cmd"
    if not config.exists():
        # A keyed digest, generated once per install and never regenerated:
        # rotating it silently would orphan every stored API key and every
        # national-identifier hash already written.
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        config.write_text(
            "@echo off\r\n"
            "rem Written by install.py. Keep this file: the key below is what\r\n"
            "rem every stored API key and national-identifier hash was made\r\n"
            "rem with, and they cannot be verified without it.\r\n"
            f"set CMDM_ID_HASH_KEY={key}\r\n"
            f'set CMDM_HOME={HERE}\r\n',
            encoding="utf-8",
        )
        say("generated config.cmd with a fresh identifier-hashing key")
    else:
        say("config.cmd already present — keeping the existing key")

    shell = HERE / "config.sh"
    if not shell.exists():
        key = ""
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("set CMDM_ID_HASH_KEY="):
                key = line.split("=", 1)[1]
        shell.write_text(
            f'export CMDM_ID_HASH_KEY="{key}"\nexport CMDM_HOME="{HERE}"\n',
            encoding="utf-8",
        )

    print("\nInstalled.\n")
    print("  Next, prove it works on this machine:")
    print("      verify.cmd" if platform.system() == "Windows" else "      ./verify.sh")
    print("\n  Then start it:")
    print("      start.cmd" if platform.system() == "Windows" else "      ./start.sh")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
