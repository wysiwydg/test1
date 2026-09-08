"""Packaging, transports and the submission ledger."""

from __future__ import annotations

import datetime as dt
import json
import zipfile

import pytest

from aml.case.store import AmlStore
from aml.config import AmlConfig, InstitutionProfile, PortalConfig
from aml.model.enums import ReportKind, SubmissionState
from aml.portal.client import Receipt, submit_package, transport_for
from aml.portal.package import MANIFEST_NAME, build_package


@pytest.fixture
def report_file(tmp_path):
    path = tmp_path / "DEMO_CTR_20260309_001.csv"
    path.write_text("REPORT_TYPE,AMOUNT_PHP\r\nCTR,600000.00\r\n", encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path) -> AmlConfig:
    return AmlConfig(
        institution=InstitutionProfile(
            covered_person_name="Demo Life",
            amlc_institution_code="DEMO-001",
            compliance_officer_name="R. Villanueva",
        ),
        portal=PortalConfig(mode="spool", spool_dir=str(tmp_path / "spool")),
        output_dir=str(tmp_path / "out"),
        store_path=str(tmp_path / "aml.sqlite3"),
    )


def test_packaging_is_deterministic(report_file, tmp_path):
    first = build_package([report_file], out_dir=tmp_path / "p1", institution_code="DEMO-001",
                          kind="CTR", day=dt.date(2026, 3, 9))
    second = build_package([report_file], out_dir=tmp_path / "p2", institution_code="DEMO-001",
                           kind="CTR", day=dt.date(2026, 3, 9))
    assert first.sha256 == second.sha256


def test_the_manifest_states_what_is_inside(report_file, tmp_path):
    package = build_package([report_file], out_dir=tmp_path / "p", institution_code="DEMO-001",
                            kind="CTR", day=dt.date(2026, 3, 9),
                            period_start=dt.date(2026, 3, 1), period_end=dt.date(2026, 3, 8))
    with zipfile.ZipFile(package.path) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))
        assert archive.namelist() == sorted(archive.namelist())
    assert manifest["institution_code"] == "DEMO-001"
    assert manifest["period_start"] == "2026-03-01"
    assert manifest["files"][0]["filename"] == report_file.name
    assert manifest["files"][0]["sha256"]


def test_encryption_round_trips_or_refuses_cleanly(report_file, tmp_path):
    from aml.portal.package import encrypt_file

    package = build_package([report_file], out_dir=tmp_path / "p", institution_code="DEMO-001",
                            kind="CTR", day=dt.date(2026, 3, 9))
    try:
        encrypted = encrypt_file(package.path, "correct horse battery staple")
    except RuntimeError as exc:
        # No usable 'cryptography': the contract is that it refuses rather than
        # writing the package in the clear.
        assert "refusing to submit unencrypted" in str(exc).lower()
        return
    from aml.portal.package import decrypt_file

    assert encrypted.encrypted
    assert decrypt_file(encrypted.path, "correct horse battery staple") == package.path.read_bytes()
    # The wrong passphrase must fail loudly rather than returning plausible
    # bytes: that is why the package is authenticated, not merely encrypted.
    try:
        decrypt_file(encrypted.path, "wrong passphrase")
    except Exception as exc:
        assert type(exc).__name__ == "InvalidTag"
    else:  # pragma: no cover - would mean the ciphertext is unauthenticated
        pytest.fail("decryption with the wrong passphrase must not succeed")


def test_a_relative_spool_directory_resolves_under_the_output_directory():
    config = AmlConfig(portal=PortalConfig(spool_dir="out/amlc-spool"), output_dir="/srv/aml/out")
    assert str(transport_for(config).spool_dir) == "/srv/aml/out/amlc-spool"


def test_spool_submission_writes_the_package_and_instructions(config, report_file):
    store = AmlStore(config.store_file())
    store.record_report(
        report_id="RPT-1",
        kind=ReportKind.CTR,
        rendered={"path": str(report_file), "filename": report_file.name, "sha256": "abc",
                  "rows": 1, "size_bytes": 40, "spec_version": "1"},
        actor="tester",
    )
    submission_id, receipt = submit_package(
        store, config, report_id="RPT-1", files=[report_file], kind=ReportKind.CTR,
        actor="co.villanueva",
    )
    assert receipt.state is SubmissionState.PACKAGED
    spool = transport_for(config).spool_dir
    assert list(spool.glob("*.zip")) and list(spool.glob("*.submission.json"))
    note = json.loads(next(spool.glob("*.submission.json")).read_text(encoding="utf-8"))
    assert "Upload this package through the AMLC portal" in note["instructions"]
    row = store.submissions()[0]
    assert row["submission_id"] == submission_id and row["package_sha256"]


def test_an_identical_package_is_not_filed_twice(config, report_file):
    store = AmlStore(config.store_file())
    store.record_report(
        report_id="RPT-1",
        kind=ReportKind.CTR,
        rendered={"path": str(report_file), "filename": report_file.name, "sha256": "abc",
                  "rows": 1, "size_bytes": 40, "spec_version": "1"},
        actor="tester",
    )

    class Accepting:
        name = "test"

        def submit(self, package, metadata):
            return Receipt(SubmissionState.ACKNOWLEDGED, reference="AMLC-1", message="ok")

        def status(self, reference):
            return Receipt(SubmissionState.ACKNOWLEDGED, reference=reference)

    first_id, first = submit_package(
        store, config, report_id="RPT-1", files=[report_file], kind=ReportKind.CTR,
        actor="co", transport=Accepting(),
    )
    second_id, second = submit_package(
        store, config, report_id="RPT-1", files=[report_file], kind=ReportKind.CTR,
        actor="co", transport=Accepting(),
    )
    assert first_id == second_id
    assert first.state is SubmissionState.ACKNOWLEDGED
    assert "already submitted" in second.message


def test_a_failed_submission_is_still_recorded(config, report_file):
    store = AmlStore(config.store_file())
    store.record_report(
        report_id="RPT-1",
        kind=ReportKind.CTR,
        rendered={"path": str(report_file), "filename": report_file.name, "sha256": "abc",
                  "rows": 1, "size_bytes": 40, "spec_version": "1"},
        actor="tester",
    )

    class Failing:
        name = "test"

        def submit(self, package, metadata):
            raise TimeoutError("portal unreachable")

        def status(self, reference):
            raise NotImplementedError

    with pytest.raises(TimeoutError):
        submit_package(store, config, report_id="RPT-1", files=[report_file],
                       kind=ReportKind.CTR, actor="co", transport=Failing())
    row = store.submissions()[0]
    assert row["state"] == str(SubmissionState.FAILED)
    assert "portal unreachable" in row["last_error"]


def test_filing_without_an_institution_profile_is_refused(tmp_path, report_file):
    bare = AmlConfig(output_dir=str(tmp_path / "out"), store_path=str(tmp_path / "s.sqlite3"))
    store = AmlStore(bare.store_file())
    with pytest.raises(ValueError, match="institution profile is incomplete"):
        submit_package(store, bare, report_id="RPT-1", files=[report_file],
                       kind=ReportKind.CTR, actor="co")
