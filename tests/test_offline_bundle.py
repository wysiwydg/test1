"""Tests for the offline bundle builder.

The bug these exist for: ``pip download --platform win_amd64`` picks wheel
*tags* for Windows but evaluates environment *markers* against the machine
doing the downloading. Every dependency guarded by ``sys_platform == "win32"``
was therefore skipped when building on Linux, and the omission surfaced on the
target -- which by definition has no network to fix it with.

The verification had the same blind spot, because it also ran on Linux.
"""

from __future__ import annotations

import email
import pathlib
import zipfile

import pytest

from scripts.build_offline_bundle import (
    _canonical,
    _marker_environment,
    _missing_for_target,
)


def wheel(tmp_path: pathlib.Path, name: str, version: str, requires: list[str]):
    """A wheel carrying nothing but the metadata the resolver reads."""
    path = tmp_path / f"{name}-{version}-py3-none-any.whl"
    message = email.message.Message()
    message["Metadata-Version"] = "2.1"
    message["Name"] = name
    message["Version"] = version
    for requirement in requires:
        message["Requires-Dist"] = requirement
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{name}-{version}.dist-info/METADATA", message.as_string())
    return path


def test_a_windows_only_dependency_is_missed_by_this_machine(tmp_path) -> None:
    """The exact failure: colorama, required by pytest only on Windows."""
    wheel(tmp_path, "pytest", "8.0", ['colorama>=0.4; sys_platform == "win32"'])

    windows = _missing_for_target(tmp_path, _marker_environment("win_amd64", "3.13"))
    linux = _missing_for_target(
        tmp_path, _marker_environment("manylinux_2_17_x86_64", "3.13")
    )

    assert any(item.startswith("colorama") for item in windows)
    assert linux == set(), "the same wheelhouse is complete for Linux, which is why "\
                           "building there hid this"


def test_a_satisfied_dependency_is_not_reported(tmp_path) -> None:
    wheel(tmp_path, "pytest", "8.0", ['colorama>=0.4; sys_platform == "win32"'])
    wheel(tmp_path, "colorama", "0.4.6", [])
    assert _missing_for_target(tmp_path, _marker_environment("win_amd64", "3.13")) == set()


def test_an_extra_gated_dependency_is_not_pulled_in(tmp_path) -> None:
    """Extras are inactive unless requested; treating them as required would
    drag optional trees into every bundle."""
    wheel(tmp_path, "psycopg", "3.2", ['pytest; extra == "dev"'])
    assert _missing_for_target(tmp_path, _marker_environment("win_amd64", "3.13")) == set()


def test_a_version_gated_dependency_follows_the_target_python(tmp_path) -> None:
    wheel(tmp_path, "pytest", "8.0", ['tomli>=1; python_version < "3.11"'])
    assert _missing_for_target(tmp_path, _marker_environment("win_amd64", "3.13")) == set()
    old = _missing_for_target(tmp_path, _marker_environment("win_amd64", "3.10"))
    assert any(item.startswith("tomli") for item in old)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win_amd64", ("win32", "Windows", "nt")),
        ("manylinux_2_17_x86_64", ("linux", "Linux", "posix")),
        ("macosx_11_0_arm64", ("darwin", "Darwin", "posix")),
    ],
)
def test_marker_environment_describes_the_target(platform, expected) -> None:
    environment = _marker_environment(platform, "3.13")
    assert (
        environment["sys_platform"],
        environment["platform_system"],
        environment["os_name"],
    ) == expected


def test_distribution_names_normalise() -> None:
    """prometheus_client and prometheus-client are the same requirement."""
    assert _canonical("prometheus_client") == _canonical("prometheus-client")
    assert _canonical("typing_extensions") == "typing-extensions"


# ---------------------------------------------------------------------------
# Update pack
# ---------------------------------------------------------------------------


def test_the_update_pack_declares_only_what_a_bundle_installs() -> None:
    """The pack is refused on the target if it needs a package the bundle does
    not carry. Declaring the dev extras there would make every update refuse
    itself over ruff and mypy, which no bundle has ever contained."""
    from scripts.build_update_pack import _declared_dependencies

    declared = _declared_dependencies()
    assert {"polars", "psycopg", "fastapi", "pytest"} <= declared
    assert not declared & {"ruff", "mypy", "onnxruntime", "pgserver"}


def test_the_updater_will_not_touch_the_irreplaceable_things() -> None:
    """pgdata is the golden store and config holds the hashing key. An update
    that overwrote either would destroy the installation it was fixing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cmdm_update", pathlib.Path(__file__).resolve().parent.parent
        / "offline" / "update.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    replaced = set(module.DIRECTORIES) | set(module.FILES)
    assert not replaced & set(module.PRESERVED)
    for name in ("pgdata", "config.cmd", "config.sh", ".venv", "wheels", "pgsql"):
        assert name in module.PRESERVED
