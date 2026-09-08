"""Packaging a report for submission, reproducibly.

A submission package is a zip containing the report files and a manifest that
states, for each one, what it is and what it hashes to. Three properties are
designed in.

**Deterministic.** Entry timestamps are fixed and entries are sorted, so
packaging the same reports twice produces byte-identical archives with the same
digest. That is what lets the institution prove, three years later, that the
file it holds is the file it sent — a zip that embeds the packaging time hashes
differently every run and proves nothing.

**Self-describing.** The manifest carries the institution code, the report
kind, the covered period, the row counts and the per-file digests. If a package
is ever separated from this system — sent to an examiner, restored from a
backup — it still says what it contains.

**Optionally encrypted.** AES-256-GCM under a key derived from a passphrase
with PBKDF2, salt and nonce in the header. This is here because reports leave
the building and because institutions increasingly require encryption at rest
for regulatory submissions. It requires ``cryptography``; the failure when it
is absent is an explicit error, never a silent fallback to plaintext.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PackagedReport", "build_package", "encrypt_file", "MANIFEST_NAME", "PACKAGE_FORMAT"]

MANIFEST_NAME = "manifest.json"
PACKAGE_FORMAT = "aml-package/1"

#: Fixed zip entry timestamp. Any constant works; this one is the earliest a
#: zip can express, so it is obviously deliberate rather than a stale clock.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)

_ENCRYPTION_HEADER = b"AMLCPKG1"


@dataclass(frozen=True, slots=True)
class PackagedReport:
    """A submission package on disk."""

    path: pathlib.Path
    sha256: str
    size_bytes: int
    files: tuple[str, ...]
    manifest: Mapping[str, Any] = field(default_factory=dict)
    encrypted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "files": list(self.files),
            "encrypted": self.encrypted,
        }


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_package(
    files: Sequence[str | pathlib.Path],
    *,
    out_dir: str | pathlib.Path,
    institution_code: str,
    kind: str,
    day: dt.date,
    sequence: int = 1,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    extra: Mapping[str, Any] | None = None,
) -> PackagedReport:
    """Zip the given report files with a manifest."""
    if not files:
        raise ValueError("nothing to package")
    target_dir = pathlib.Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{institution_code or 'INSTITUTION'}_{kind}_{day.strftime('%Y%m%d')}_{sequence:03d}.zip"
    target = target_dir / name

    entries = []
    payloads: dict[str, bytes] = {}
    for path in sorted(pathlib.Path(p) for p in files):
        payload = path.read_bytes()
        payloads[path.name] = payload
        entries.append(
            {
                "filename": path.name,
                "sha256": _digest_bytes(payload),
                "size_bytes": len(payload),
            }
        )

    manifest: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "institution_code": institution_code,
        "report_kind": kind,
        "package_date": day.isoformat(),
        "sequence": sequence,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "files": entries,
        **(dict(extra) if extra else {}),
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(payloads):
            info = zipfile.ZipInfo(filename, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payloads[filename])
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=_FIXED_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, manifest_bytes)

    payload = target.read_bytes()
    return PackagedReport(
        path=target,
        sha256=_digest_bytes(payload),
        size_bytes=len(payload),
        files=tuple(sorted(payloads)) + (MANIFEST_NAME,),
        manifest=manifest,
    )


def encrypt_file(
    path: str | pathlib.Path, passphrase: str, *, iterations: int = 240_000
) -> PackagedReport:
    """Encrypt a package with AES-256-GCM under a passphrase-derived key.

    Layout: ``AMLCPKG1 | salt(16) | nonce(12) | ciphertext+tag``. GCM rather
    than CBC so tampering is detected on decryption rather than producing
    plausible garbage, which matters for a file whose integrity is the point.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except Exception as exc:  # noqa: BLE001 - a broken install fails as loudly as a missing one
        # Deliberately not ImportError alone: a half-installed ``cryptography``
        # (bindings present, backend missing) raises something else entirely,
        # and the outcome must be the same either way — refuse, never fall back
        # to writing the package in the clear.
        raise RuntimeError(
            "package encryption requires a working 'cryptography' install; "
            f"importing it failed with {type(exc).__name__}: {exc}. Install it, or set "
            "portal.encrypt = false. Refusing to submit unencrypted when encryption "
            "was requested."
        ) from exc

    import os

    source = pathlib.Path(path)
    plaintext = source.read_bytes()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(passphrase.encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _ENCRYPTION_HEADER)
    payload = _ENCRYPTION_HEADER + salt + nonce + ciphertext
    target = source.with_suffix(source.suffix + ".enc")
    target.write_bytes(payload)
    return PackagedReport(
        path=target,
        sha256=_digest_bytes(payload),
        size_bytes=len(payload),
        files=(source.name,),
        manifest={"format": PACKAGE_FORMAT, "encryption": "AES-256-GCM/PBKDF2-SHA256"},
        encrypted=True,
    )


def decrypt_file(
    path: str | pathlib.Path, passphrase: str, *, iterations: int = 240_000
) -> bytes:
    """Decrypt a package produced by :func:`encrypt_file`."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    payload = pathlib.Path(path).read_bytes()
    if not payload.startswith(_ENCRYPTION_HEADER):
        raise ValueError("not an AML package: header missing")
    body = payload[len(_ENCRYPTION_HEADER):]
    salt, nonce, ciphertext = body[:16], body[16:28], body[28:]
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(passphrase.encode("utf-8"))
    plaintext: bytes = AESGCM(key).decrypt(nonce, ciphertext, _ENCRYPTION_HEADER)
    return plaintext
