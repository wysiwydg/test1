"""Identifier generation and content hashing.

Two kinds of identity live here, and keeping them apart matters.

**Surrogate golden keys** are UUIDv7: a millisecond timestamp in the high bits
followed by random bits. They are opaque and carry no business meaning, but they
sort by creation time, which is worth more than it sounds. Random UUIDv4 keys
scatter B-tree inserts across the whole index and make every bulk load a
random-write workload; time-ordered keys append. The same property keeps rows
that arrived together physically together in Parquet row groups, so a
recently-loaded batch reads back without touching the whole file.

**Content hashes** are deterministic BLAKE2b digests. They answer "is this the
same content I already have", which underpins re-delivery detection in the
landing zone and change detection in the golden layer. BLAKE2b rather than
SHA-256 because it is faster on the long strings this system hashes constantly
and supports keying natively, which the identifier hashing needs.

Standard library only. This module is imported by every worker in the system and
should never be a reason to install anything.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "uuid7",
    "uuid7_at",
    "record_hash",
    "payload_hash",
    "keyed_identifier_hash",
    "blocking_hash",
    "NULL_UUID",
]

#: Sentinel used where a UUID column must be non-null but no entity exists yet,
#: such as an edge staged before its target has been resolved.
NULL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")

#: Separator for hash inputs. Chosen as a byte that cannot occur in normalized
#: text, so that hashing ("ab", "c") and ("a", "bc") cannot collide.
_SEP = b"\x1f"

_HASH_DIGEST_SIZE = 16  # 128 bits, rendered as 32 hex chars.


def uuid7() -> uuid.UUID:
    """Generate a time-ordered UUIDv7 for the current instant.

    Layout follows RFC 9562: 48 bits of Unix time in milliseconds, 4 bits of
    version, 12 bits of randomness, 2 bits of variant, 62 bits of randomness.
    Two calls in the same millisecond are ordered arbitrarily relative to each
    other but still sort into the correct millisecond bucket, which is all the
    index locality argument requires.
    """
    return uuid7_at(time.time_ns() // 1_000_000)


def uuid7_at(unix_ms: int) -> uuid.UUID:
    """Generate a UUIDv7 stamped with a specific millisecond timestamp.

    Exposed separately so that a backfill can generate keys that sort into the
    period the data belongs to rather than the period it was loaded in, and so
    that tests can produce deterministic orderings.
    """
    if not 0 <= unix_ms < 1 << 48:
        raise ValueError(f"unix_ms out of range for UUIDv7: {unix_ms}")

    rand = secrets.token_bytes(10)
    # Assembled as one expression rather than a chain of in-place ors, which is
    # easier to check field-by-field against the RFC layout.
    rand_a = int.from_bytes(rand[0:2], "big") & 0x0FFF
    rand_b = int.from_bytes(rand[2:10], "big") & ((1 << 62) - 1)
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | rand_a << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


def _canonical_bytes(value: Any) -> bytes:
    """Render a value into a stable byte form for hashing.

    Stability across processes and Python versions is the whole point, so this
    deliberately avoids ``repr``, ``hash`` and pickle, all of which are free to
    change their output. Nulls hash distinctly from empty strings, because in
    this model "unknown" and "known to be blank" are different facts.
    """
    if value is None:
        return b"\x00NULL"
    if isinstance(value, bool):
        return b"\x01T" if value else b"\x01F"
    if isinstance(value, (int,)):
        return b"\x02" + str(value).encode("utf-8")
    if isinstance(value, float):
        # repr round-trips exactly for float and is stable across CPython
        # versions; format() with a fixed precision would silently truncate.
        return b"\x03" + repr(value).encode("utf-8")
    if isinstance(value, uuid.UUID):
        return b"\x04" + value.bytes
    if isinstance(value, bytes):
        return b"\x05" + value
    if isinstance(value, str):
        return b"\x06" + value.encode("utf-8")
    if isinstance(value, Mapping):
        parts = [b"\x07"]
        for k in sorted(value):
            parts.append(_canonical_bytes(k))
            parts.append(_canonical_bytes(value[k]))
        return _SEP.join(parts)
    if isinstance(value, (list, tuple)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        return _SEP.join([b"\x08", *(_canonical_bytes(v) for v in value)])
    # Dates, datetimes, Decimals and anything else with a stable isoformat or
    # str. Decimal("1.50") and Decimal("1.5") hash differently, which is correct
    # for money: the scale is part of the asserted value.
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return b"\x09" + iso().encode("utf-8")
    return b"\x0a" + str(value).encode("utf-8")


def record_hash(values: Mapping[str, Any], fields: Iterable[str]) -> str:
    """Hash the named fields of a record, for change detection.

    Field names are hashed alongside their values so that renaming a column or
    reordering the field list changes the digest. That is intentional: a schema
    change should invalidate cached hashes rather than let stale rows look
    unchanged.

    ``fields`` is passed in rather than taken from the mapping's keys so that
    system and audit columns are excluded deterministically. A hash that
    included ``updated_at`` would differ on every ingest and defeat the purpose.
    """
    h = hashlib.blake2b(digest_size=_HASH_DIGEST_SIZE)
    for name in fields:
        h.update(_canonical_bytes(name))
        h.update(_SEP)
        h.update(_canonical_bytes(values.get(name)))
        h.update(_SEP)
    return h.hexdigest()


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash a raw source payload for re-delivery detection.

    Unlike :func:`record_hash` this covers every key present, because the
    landing zone has no notion of which fields matter yet.
    """
    h = hashlib.blake2b(_canonical_bytes(payload), digest_size=_HASH_DIGEST_SIZE)
    return h.hexdigest()


def keyed_identifier_hash(value: str, *, id_type: str, key: bytes | None = None) -> str:
    """Hash a government identifier under a secret key.

    Keyed rather than plain, because the space of national identifiers is small
    enough to enumerate: an unkeyed digest of a nine-digit number is reversible
    by anyone with a laptop and is not a protection at all. The key lives in the
    environment as ``CMDM_ID_HASH_KEY`` and never in the database, so a dump of
    the golden store does not carry the means to reverse these values.

    The identifier type is mixed in so that the same digits registered as two
    different kinds of identifier do not collide into a false match.
    """
    if key is None:
        env = os.environ.get("CMDM_ID_HASH_KEY")
        if not env:
            raise RuntimeError(
                "CMDM_ID_HASH_KEY is not set. National identifier hashing requires a "
                "secret key; an unkeyed hash of an identifier is trivially reversible."
            )
        key = env.encode("utf-8")

    normalized = "".join(ch for ch in value.upper() if ch.isalnum())
    if not normalized:
        raise ValueError("identifier is empty after normalization")

    h = hashlib.blake2b(key=key[:64], digest_size=_HASH_DIGEST_SIZE)
    h.update(_canonical_bytes(id_type))
    h.update(_SEP)
    h.update(_canonical_bytes(normalized))
    return h.hexdigest()


def blocking_hash(*parts: str | None) -> str:
    """Short unkeyed digest for blocking keys such as the address key.

    Unkeyed and short on purpose. These are join keys, not secrets; they compare
    equal only for records that were already equal after normalization, and a
    64-bit digest keeps the index small while collisions remain harmless. A
    blocking collision costs one extra pair for the scorer to reject, which is
    the cheapest possible failure mode.
    """
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update(_canonical_bytes(p))
        h.update(_SEP)
    return h.hexdigest()
