"""Getting a package to the AMLC, and proving that it got there.

Two transports, and the default is the honest one.

``spool`` writes the finished package and its receipt stub into a directory,
ready for a person to upload through the portal in a browser. This is not a
placeholder: an institution that has not completed portal enrolment, or whose
security policy forbids an automated upload from a system holding client data,
is fully served by it — the package is byte-identical to what the HTTP
transport would send, and the ledger records it the same way.

``http`` drives the portal directly. Its endpoint paths, the shape of its login
and the form field names are properties of the institution's own enrolment, and
they live in configuration rather than in this code, because there is no version
of this file that can be right for every enrolment. What is fixed here is the
part that is genuinely general: authenticate, upload the package as multipart
form data, keep the receipt, poll for acknowledgement, retry the failures worth
retrying, and never lose the audit record of an attempt.

**Idempotency.** A submission's identity is derived from the digest of the
package. Re-running submission with the same content finds the existing
submission and refuses to file it twice — because filing the same CTRs twice is
a data-quality finding, and because retry after a timeout is exactly when it
would otherwise happen.
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import pathlib
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from aml.config import AmlConfig, PortalConfig, resolve_secret
from aml.http import HttpClient, HttpError
from aml.model.enums import ReportKind, SubmissionState
from aml.phcalendar import now_manila
from aml.portal.package import PackagedReport, build_package, encrypt_file

__all__ = ["Receipt", "SpoolTransport", "AmlcPortalClient", "transport_for", "submit_package"]


@dataclass(frozen=True, slots=True)
class Receipt:
    """What the far end said."""

    state: SubmissionState
    reference: str = ""
    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "reference": self.reference,
            "message": self.message,
            "raw": dict(self.raw),
        }


@runtime_checkable
class PortalTransport(Protocol):
    name: str

    def submit(self, package: PackagedReport, metadata: Mapping[str, Any]) -> Receipt:
        ...

    def status(self, reference: str) -> Receipt:
        ...


class SpoolTransport:
    """Write the package where a person can pick it up and upload it."""

    name = "spool"

    def __init__(self, spool_dir: str | pathlib.Path) -> None:
        self.spool_dir = pathlib.Path(spool_dir)

    def submit(self, package: PackagedReport, metadata: Mapping[str, Any]) -> Receipt:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / package.path.name
        if target.resolve() != package.path.resolve():
            target.write_bytes(package.path.read_bytes())
        note = self.spool_dir / f"{package.path.stem}.submission.json"
        payload = {
            "package": package.as_dict(),
            "metadata": dict(metadata),
            "prepared_at": now_manila().isoformat(),
            "instructions": (
                "Upload this package through the AMLC portal under the institution's "
                "enrolment, then record the portal's acknowledgement reference with "
                "'aml submit --acknowledge'."
            ),
        }
        note.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return Receipt(
            state=SubmissionState.PACKAGED,
            reference="",
            message=f"package written to {target}; upload it through the portal",
            raw={"package_path": str(target), "note": str(note)},
        )

    def status(self, reference: str) -> Receipt:
        return Receipt(
            state=SubmissionState.PACKAGED,
            reference=reference,
            message="spool transport does not track portal state; acknowledge manually",
        )


class AmlcPortalClient:
    """HTTP transport for the AMLC portal.

    The request shapes below are the general case — a session login returning a
    token, a multipart upload, a status endpoint keyed by the portal's
    reference. Paths and credentials come from ``[portal]`` in the
    configuration. Confirm them against the institution's enrolment
    documentation before switching ``mode`` to ``http``; nothing else in the
    package changes when you do.
    """

    name = "http"

    def __init__(self, config: PortalConfig, client: HttpClient | None = None) -> None:
        if not config.base_url:
            raise ValueError(
                "portal.mode is 'http' but portal.base_url is empty; "
                "configure the portal endpoint or use mode = 'spool'"
            )
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or HttpClient(
            timeout=config.request_timeout_seconds,
            max_retries=config.max_retries,
            verify_tls=config.verify_tls,
            client_cert=config.client_cert,
            client_key=config.client_key,
        )
        self._token = ""

    # -- session ---------------------------------------------------------

    def _authenticate(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        username = resolve_secret(self.config.username, label="AMLC portal username")
        password = resolve_secret(self.config.password, label="AMLC portal password")
        if not username or not password:
            raise HttpError("AMLC portal credentials are not configured")
        response = self.client.post_json(
            f"{self.base_url}{self.config.login_path}",
            {"username": username, "password": password},
        )
        payload = response.json() or {}
        token = payload.get("token") or payload.get("access_token") or payload.get("sessionId")
        if not token:
            raise HttpError("portal login returned no session token", status=response.status)
        self._token = str(token)
        return {"Authorization": f"Bearer {self._token}"}

    # -- submission ------------------------------------------------------

    def submit(self, package: PackagedReport, metadata: Mapping[str, Any]) -> Receipt:
        boundary = f"----aml{secrets.token_hex(16)}"
        body = _multipart(
            boundary,
            fields={str(k): str(v) for k, v in metadata.items() if v is not None},
            filename=package.path.name,
            payload=package.path.read_bytes(),
        )
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "Accept": "application/json",
            # The digest travels with the upload so the portal can reject a
            # truncated transfer rather than accepting half a report.
            "X-Content-SHA256": package.sha256,
            **self._authenticate(),
        }
        response = self.client.request(
            "POST", f"{self.base_url}{self.config.upload_path}", headers=headers, body=body
        )
        payload = response.json() or {}
        reference = str(
            payload.get("submissionReference")
            or payload.get("reference")
            or payload.get("id")
            or ""
        )
        acknowledged = str(payload.get("status", "")).upper() in {
            "ACCEPTED",
            "ACKNOWLEDGED",
            "RECEIVED",
        }
        return Receipt(
            state=SubmissionState.ACKNOWLEDGED if acknowledged else SubmissionState.SUBMITTED,
            reference=reference,
            message=str(payload.get("message", "")) or f"HTTP {response.status}",
            raw=payload if isinstance(payload, Mapping) else {"response": str(payload)},
        )

    def status(self, reference: str) -> Receipt:
        path = self.config.status_path.format(submission_reference=reference)
        response = self.client.get(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", **self._authenticate()},
        )
        payload = response.json() or {}
        status = str(payload.get("status", "")).upper()
        state = {
            "ACCEPTED": SubmissionState.ACKNOWLEDGED,
            "ACKNOWLEDGED": SubmissionState.ACKNOWLEDGED,
            "RECEIVED": SubmissionState.ACKNOWLEDGED,
            "REJECTED": SubmissionState.REJECTED,
            "FAILED": SubmissionState.FAILED,
        }.get(status, SubmissionState.SUBMITTED)
        return Receipt(
            state=state,
            reference=reference,
            message=str(payload.get("message", "")) or status,
            raw=payload if isinstance(payload, Mapping) else {},
        )


def _multipart(
    boundary: str, *, fields: Mapping[str, str], filename: str, payload: bytes
) -> bytes:
    """Build a multipart/form-data body.

    Hand-rolled because the standard library has no encoder for it and this
    package does not take a dependency to gain one.
    """
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts)


def transport_for(config: AmlConfig) -> PortalTransport:
    """The transport the configuration asks for."""
    mode = config.portal.mode.strip().lower()
    if mode == "spool":
        # A relative spool directory belongs under the configured output
        # directory, not under whatever directory the command was run from.
        # Otherwise the package a scheduler produced at 02:00 is wherever cron
        # happened to be standing.
        spool = pathlib.Path(config.portal.spool_dir)
        if not spool.is_absolute():
            output = config.output_path()
            # "out/amlc-spool" next to an output directory of "out" means one
            # directory, not "out/out/amlc-spool".
            root = output.parent if spool.parts[:1] == (output.name,) else output
            spool = root / spool
        return SpoolTransport(spool)
    if mode == "http":
        return AmlcPortalClient(config.portal)
    raise ValueError(f"unknown portal mode {config.portal.mode!r}; use 'spool' or 'http'")


def submit_package(
    store: Any,
    config: AmlConfig,
    *,
    report_id: str,
    files: Sequence[str | pathlib.Path],
    kind: ReportKind,
    actor: str,
    day: dt.date | None = None,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    transport: PortalTransport | None = None,
    force: bool = False,
) -> tuple[str, Receipt]:
    """Package, submit and record. Returns the submission id and the receipt.

    Refuses to file the same package twice unless ``force`` is set, and records
    every attempt — including the failures — against the submission in the
    ledger.
    """
    missing = config.institution.missing_for_filing()
    if missing:
        raise ValueError(
            "the institution profile is incomplete and a report cannot be filed without it: "
            + ", ".join(missing)
        )

    when = day or now_manila().date()
    package = build_package(
        files,
        out_dir=config.output_path() / "packages",
        institution_code=config.institution.amlc_institution_code,
        kind=str(kind),
        day=when,
        period_start=period_start,
        period_end=period_end,
        extra={
            "covered_person": config.institution.covered_person_name,
            "compliance_officer": config.institution.compliance_officer_name,
            "report_id": report_id,
        },
    )
    if config.portal.encrypt:
        passphrase = resolve_secret(
            config.portal.encryption_passphrase, label="package passphrase"
        )
        if not passphrase:
            raise ValueError("portal.encrypt is on but no passphrase is configured")
        package = encrypt_file(package.path, passphrase)

    submission_id = f"SUB-{package.sha256[:12].upper()}"
    existing = {row["submission_id"]: row for row in store.submissions(limit=1000)}
    prior = existing.get(submission_id)
    if prior and not force and prior["state"] in (
        str(SubmissionState.SUBMITTED),
        str(SubmissionState.ACKNOWLEDGED),
    ):
        return submission_id, Receipt(
            state=SubmissionState(prior["state"]),
            reference=prior["receipt_reference"],
            message="already submitted; identical package digest",
            raw=dict(prior),
        )

    store.record_submission(
        submission_id=submission_id,
        report_id=report_id,
        state=SubmissionState.PACKAGED,
        mode=config.portal.mode,
        actor=actor,
        package_path=str(package.path),
        package_sha256=package.sha256,
    )

    carrier = transport or transport_for(config)
    metadata = {
        "institutionCode": config.institution.amlc_institution_code,
        "reportType": str(kind),
        "reportDate": when.isoformat(),
        "fileName": package.path.name,
        "sha256": package.sha256,
        "contactPerson": config.institution.compliance_officer_name,
    }
    try:
        receipt = carrier.submit(package, metadata)
    except Exception as exc:  # noqa: BLE001 - the attempt must be recorded whatever happened
        store.record_submission(
            submission_id=submission_id,
            report_id=report_id,
            state=SubmissionState.FAILED,
            mode=config.portal.mode,
            actor=actor,
            package_path=str(package.path),
            package_sha256=package.sha256,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    now = now_manila()
    store.record_submission(
        submission_id=submission_id,
        report_id=report_id,
        state=receipt.state,
        mode=config.portal.mode,
        actor=actor,
        package_path=str(package.path),
        package_sha256=package.sha256,
        receipt_reference=receipt.reference,
        submitted_at=now if receipt.state is not SubmissionState.PACKAGED else None,
        acknowledged_at=now if receipt.state is SubmissionState.ACKNOWLEDGED else None,
    )
    return submission_id, receipt
