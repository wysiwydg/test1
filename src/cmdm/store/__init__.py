"""The golden record store: SCD-2 writes, crosswalk, provenance."""

from cmdm.store.writer import (
    WriteReport,
    resolve_person_id,
    upsert_policy_xref,
    upsert_xref,
    write_entities,
)

__all__ = [
    "write_entities",
    "upsert_xref",
    "upsert_policy_xref",
    "resolve_person_id",
    "WriteReport",
]
