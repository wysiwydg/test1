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


def test_the_update_pack_declares_only_what_a_bundle_needs_to_run() -> None:
    """The pack is refused on the target if it needs a package the bundle does
    not carry. Declaring the dev extras there would make every update refuse
    itself over ruff and mypy, which no bundle has ever contained."""
    from scripts.build_update_pack import _declared_dependencies

    declared = _declared_dependencies()
    assert {"polars", "psycopg", "fastapi", "pytest"} <= declared
    assert not declared & {"ruff", "mypy", "pgserver"}


def test_an_optional_runtime_does_not_refuse_an_older_bundle() -> None:
    """onnxruntime ships in a bundle now, so a model can be promoted on a
    machine with no internet. It is still not *required*: nothing imports it
    until a model is ACTIVE. Every bundle cut before it was added would be
    refused this release over a package its code never reaches for — and the
    refusal is meant for the case where the update would genuinely not run."""
    from scripts.build_offline_bundle import OPTIONAL, OPTIONAL_AT_RUNTIME
    from scripts.build_update_pack import _declared_dependencies

    assert any(spec.startswith("onnxruntime") for spec in OPTIONAL), (
        "an update pack cannot install a compiled dependency, so a bundle "
        "without onnxruntime can never run an ONNX model however it is approved"
    )
    assert "onnxruntime" in OPTIONAL_AT_RUNTIME
    assert "onnxruntime" not in _declared_dependencies()


def test_the_pack_never_ships_the_reflex_build_tree_or_session_state() -> None:
    """Two things live under rxapp/ that belong to a build host and nowhere
    else. `.web` is 200 MB of node_modules, in a pack whose entire reason for
    existing is that it is small. `.states` is Reflex's pickled live sessions --
    including, after any local sign-in, the API key that was pasted in, which
    would put one machine's credentials in every copy of the bundle."""
    import inspect

    from scripts import build_offline_bundle, build_update_pack

    for module in (build_offline_bundle, build_update_pack):
        source = inspect.getsource(module.build)
        assert '".web"' in source, f"{module.__name__} would ship node_modules"
        assert '".states"' in source, f"{module.__name__} would ship session state"


def test_the_updater_will_not_touch_the_irreplaceable_things() -> None:
    """pgdata is the golden store and config holds the hashing key. An update
    that overwrote either would destroy the installation it was fixing."""
    import importlib.util

    # In the repository the script lives under offline/; in an installed bundle
    # it sits at the root beside verify.py, and the bundle runs this same suite.
    root = pathlib.Path(__file__).resolve().parent.parent
    candidates = [root / "offline" / "update.py", root / "update.py"]
    source = next((c for c in candidates if c.exists()), None)
    if source is None:  # pragma: no cover - neither layout
        pytest.skip("update.py not present in this layout")

    spec = importlib.util.spec_from_file_location("cmdm_update", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name in ("pgdata", "pgpassword", "config.cmd", "config.sh", ".venv",
                 "wheels", "pgsql"):
        assert name in module.PRESERVED

    # The payload is whatever the pack holds, so the guarantee cannot be a
    # static comparison of two lists -- it is that the updater refuses a pack
    # carrying any of these names rather than deciding which one to overwrite.
    assert "PRESERVED" in pathlib.Path(module.__file__).read_text(encoding="utf-8")
    assert set(module.OWN) >= {"update.py", "wheels", "MANIFEST.sha256"}


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------


def _verify_module():
    import importlib.util

    root = pathlib.Path(__file__).resolve().parent.parent
    source = next(
        (c for c in (root / "offline" / "verify.py", root / "verify.py") if c.exists()),
        None,
    )
    if source is None:  # pragma: no cover - neither layout
        pytest.skip("verify.py not present in this layout")

    spec = importlib.util.spec_from_file_location("cmdm_verify", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_verifier_runs_in_a_database_it_owns() -> None:
    """It used to run in the app's own store: it TRUNCATEd the landing zone to
    make room for the sample -- destroying the delivered bytes the whole system
    is designed to be rebuildable from -- and then asserted counts over the
    whole store, which only hold if nothing else is in it. On a working
    installation that failed with "expected 15,000 role edges, got 29,985" and
    blamed the machine for the verifier's own arithmetic."""
    module = _verify_module()
    source = (pathlib.Path(module.__file__)).read_text(encoding="utf-8")

    assert module.SCRATCH_DATABASE

    # Executed SQL only. The word appears in the comment explaining why it no
    # longer does, and a check that failed on its own explanation would be an
    # incentive to delete the explanation.
    executed = [
        line for line in source.splitlines()
        if "TRUNCATE" in line.upper() and not line.lstrip().startswith(("#", "*", '"'))
    ]
    assert not executed, (
        "the verifier truncates something; it runs in its own database and has "
        f"nothing of its own to clear: {executed}"
    )


def test_the_verifier_scratch_database_is_not_the_app_store() -> None:
    """The name has to differ from the default database, or 'its own database'
    is the app's database under another description."""
    module = _verify_module()
    assert module.SCRATCH_DATABASE != "cmdm"


def test_the_edge_assertion_is_stated_as_a_relationship() -> None:
    """`edges == 15000` is a fact about one sample's size. `edges == policies *
    3` is the claim actually being made -- three parties on every policy -- and
    survives the sample changing while still failing on a missing role."""
    module = _verify_module()
    source = (pathlib.Path(module.__file__)).read_text(encoding="utf-8")
    assert "result.policies * 3" in source
    assert "edges == 15000" not in source


def test_the_updater_installs_whatever_the_pack_carries(tmp_path) -> None:
    """The bug this replaces: the builder shipped `scripts/` and the updater's
    hardcoded list did not name it, so the pack carried a directory nobody
    installed. New tests landed beside the old generator they import ground
    truth from, and the bundle failed its own verification with an ImportError
    after an update that had reported success.

    Two lists in two files that must agree will drift. This asserts there is
    only one: whatever is in the pack, minus the updater's own machinery.
    """
    module = _update_module()

    for name in ("src", "tests", "scripts", "pyproject.toml", "a_future_release_dir"):
        (tmp_path / name).mkdir() if "." not in name else (tmp_path / name).touch()
    for name in module.OWN:
        target = tmp_path / name
        target.mkdir() if name in ("wheels", "__pycache__") else target.touch()

    carried = {p.name for p in module.payload(tmp_path)}
    assert "a_future_release_dir" in carried, (
        "the updater skipped a directory the pack carried; it is keeping its "
        "own list again"
    )
    assert carried == {"src", "tests", "scripts", "pyproject.toml",
                       "a_future_release_dir"}


def test_the_pack_ships_what_the_test_suite_needs_to_run(tmp_path) -> None:
    """The bundle runs this suite to verify itself, and the suite imports the
    sample generator for ground truth and reads pytest settings from
    pyproject. A pack without them installs tests that cannot run."""
    from scripts.build_update_pack import PAYLOAD

    assert "tests" in PAYLOAD
    assert "scripts" in PAYLOAD, "the suite imports the sample generator"
    assert "pyproject.toml" in PAYLOAD, "it carries the pytest configuration"


def _update_module():
    import importlib.util

    root = pathlib.Path(__file__).resolve().parent.parent
    source = next(
        (c for c in (root / "offline" / "update.py", root / "update.py") if c.exists()),
        None,
    )
    if source is None:  # pragma: no cover - neither layout
        pytest.skip("update.py not present in this layout")
    spec = importlib.util.spec_from_file_location("cmdm_update", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bundle_does_not_carry_the_updater() -> None:
    """It would carry the copy that existed the day it was built, and an update
    pack cannot replace it -- the updater excludes itself from its own payload,
    since a script overwriting itself mid-run is not a thing to arrange. A
    bundle keeping a stale updater at its root is one somebody eventually runs.
    """
    from scripts.build_offline_bundle import UPDATER

    assert set(UPDATER) >= {"update.py", "update.cmd", "update.sh"}

    module = _update_module()
    assert not set(module.OWN) & {"verify.py", "worker.cmd", "worker.sh"}, (
        "the updater is excluding a bundle script from its payload; only its "
        "own machinery belongs in OWN"
    )
