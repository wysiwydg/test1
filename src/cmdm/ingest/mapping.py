"""Declarative source-to-canonical mapping.

A source mapping says which inbound column feeds which canonical field, and
which transform to apply on the way. It is data, held in a TOML file, not code:
onboarding a new administration platform should be a file someone can review in
a pull request without reading Python, and the mapping for a feed that changed
its column names should be diffable against the one it replaced.

Mappings are **validated against the field registry** at load time. A mapping
that names a canonical field the model does not declare fails immediately with
the offending name, rather than producing a frame with a stray column that
something downstream silently drops. That check is what keeps step 1 and step 2
honest with each other.

TOML via the standard library's ``tomllib`` rather than YAML, so the mapping
format costs no dependency. The structure is deliberately shallow — a policy
block and a repeated party block — because mapping files are read far more often
than they are written.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cmdm.model.enums import PartyRole
from cmdm.model.fields import PERSON, POLICY, EntitySpec

__all__ = [
    "Transform",
    "FieldMapping",
    "PartyMapping",
    "SourceMapping",
    "load_mapping",
    "parse_mapping",
]


class Transform:
    """Names of the transforms a mapping may request.

    Kept as string constants rather than an enum because they appear in
    hand-written TOML, where a typo should produce a clear "unknown transform"
    error listing the valid names — which is easier to give from a plain set.
    """

    TEXT = "text"
    """Trim only. For codes and identifiers that must survive verbatim."""

    NAME = "name"
    """Full-name handling: normalization, blocking keys, component inference."""

    EMAIL = "email"
    PHONE = "phone"
    POLICY_NUMBER = "policy_number"
    DATE = "date"
    MONEY = "money"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"
    """Map raw values onto a controlled vocabulary via the ``vocabulary`` table."""

    ALL = frozenset(
        {TEXT, NAME, EMAIL, PHONE, POLICY_NUMBER, DATE, MONEY, INTEGER, BOOLEAN, ENUM}
    )


#: Values a source may use for boolean true. Compared case-insensitively after
#: trimming. Anything else that is non-null reads as false.
TRUE_TOKENS = frozenset({"Y", "YES", "T", "TRUE", "1", "X"})


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One canonical field, and where its value comes from."""

    canonical: str
    #: Inbound column name. Null when ``literal`` supplies the value instead.
    source: str | None = None
    transform: str = Transform.TEXT
    #: Raw-to-canonical value map, required when transform is ``enum``.
    vocabulary: Mapping[str, str] = field(default_factory=dict)
    #: Constant value, for feeds where a canonical field is implied by the file
    #: rather than carried in it — a single-currency book, for instance.
    literal: str | None = None

    def __post_init__(self) -> None:
        if self.transform not in Transform.ALL:
            raise ValueError(
                f"{self.canonical}: unknown transform {self.transform!r}; "
                f"valid: {sorted(Transform.ALL)}"
            )
        if self.source is None and self.literal is None:
            raise ValueError(f"{self.canonical}: needs either 'source' or 'literal'")
        if self.source is not None and self.literal is not None:
            raise ValueError(f"{self.canonical}: 'source' and 'literal' are mutually exclusive")
        if self.transform == Transform.ENUM and not self.vocabulary:
            raise ValueError(f"{self.canonical}: enum transform requires a 'vocabulary' table")


@dataclass(frozen=True, slots=True)
class PartyMapping:
    """One party role carried inside a policy row.

    A policy record holds several parties side by side — owner columns, insured
    columns, agent columns. Each of these blocks describes one of them, so the
    shredder can turn one wide row into several Person rows without the source
    layout being hard-coded anywhere.
    """

    role: PartyRole
    #: Inbound column holding the source identifier for this party:
    #: OwnerCustomerId, InsuredCustomerId or AgentCode.
    key_field: str
    #: Namespace the identifier belongs to, stored on both the crosswalk and the
    #: edge. Part of the key, because the same literal can be valid in more than
    #: one namespace for different parties.
    key_kind: str
    fields: Sequence[FieldMapping] = ()
    #: Ordinal for a repeated role, distinguishing first insured from second.
    role_sequence: int = 1

    def __post_init__(self) -> None:
        if not any(f.canonical == "full_name" for f in self.fields):
            raise ValueError(
                f"party block for role {self.role}: no mapping for 'full_name'. "
                "A party with no name cannot be resolved or reviewed."
            )


@dataclass(frozen=True, slots=True)
class SourceMapping:
    """Everything needed to read one source system's policy extract."""

    source_system: str
    policy: Sequence[FieldMapping]
    parties: Sequence[PartyMapping]
    #: Date formats to try, in order. Per-source rather than global, because
    #: day-first and month-first are mutually ambiguous and no amount of
    #: inspecting the values resolves it reliably — the source owner knows.
    date_formats: Sequence[str] = ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d")
    #: Country code assumed for phone numbers that carry no international
    #: prefix.
    default_country_code: str = "1"
    #: Column holding the source's own last-changed timestamp, which drives
    #: MOST_RECENT survivorship. When absent, ingest time is used instead and
    #: the record is flagged, because a feed with no change timestamp silently
    #: degrades every recency-based rule.
    source_timestamp_field: str | None = None

    def __post_init__(self) -> None:
        if not any(f.canonical == "policy_number" for f in self.policy):
            raise ValueError(f"{self.source_system}: no mapping for 'policy_number'")
        roles = [(p.role, p.role_sequence) for p in self.parties]
        if len(roles) != len(set(roles)):
            raise ValueError(
                f"{self.source_system}: duplicate (role, role_sequence) in party blocks. "
                "Use role_sequence to distinguish a repeated role."
            )

    @property
    def source_columns(self) -> tuple[str, ...]:
        """Every inbound column the mapping reads.

        Used to check an arriving file against the mapping before any work
        starts, so a renamed column is reported as a named missing column rather
        than as a frame full of nulls.
        """
        cols: list[str] = []
        for f in self.policy:
            if f.source:
                cols.append(f.source)
        for party in self.parties:
            cols.append(party.key_field)
            for f in party.fields:
                if f.source:
                    cols.append(f.source)
        if self.source_timestamp_field:
            cols.append(self.source_timestamp_field)
        return tuple(dict.fromkeys(cols))

    def missing_columns(self, available: Sequence[str]) -> tuple[str, ...]:
        """Mapped columns absent from an arriving file."""
        present = set(available)
        return tuple(c for c in self.source_columns if c not in present)


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def _validate_canonical(names: Sequence[str], spec: EntitySpec, context: str) -> None:
    """Fail on any canonical name the registry does not declare.

    Derived fields are rejected as mapping targets too. A source cannot supply
    ``name_phonetic_key`` — it is computed here — and a mapping that claims
    otherwise is a misunderstanding worth catching at load time rather than
    letting a source value silently overwrite a derived key.
    """
    declared = spec.by_name
    for name in names:
        if name not in declared:
            raise ValueError(
                f"{context}: {name!r} is not a field of {spec.name}. "
                f"Check src/cmdm/model/fields.py for the canonical names."
            )
        if declared[name].derived:
            raise ValueError(
                f"{context}: {name!r} is derived and computed by the pipeline; "
                "it cannot be mapped from a source column."
            )


def _field_mappings(raw: Any, context: str) -> tuple[FieldMapping, ...]:
    """Build field mappings from a TOML table.

    Two spellings are accepted, because most mappings are trivial and should
    look it: ``canonical = "SourceColumn"`` for a plain text copy, and
    ``canonical = {source = "...", transform = "..."}`` when more is needed.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{context}: expected a table, got {type(raw).__name__}")

    out: list[FieldMapping] = []
    for canonical, value in raw.items():
        if isinstance(value, str):
            out.append(FieldMapping(canonical=canonical, source=value))
        elif isinstance(value, dict):
            unknown = set(value) - {"source", "transform", "vocabulary", "literal"}
            if unknown:
                raise ValueError(f"{context}.{canonical}: unknown keys {sorted(unknown)}")
            out.append(
                FieldMapping(
                    canonical=canonical,
                    source=value.get("source"),
                    transform=value.get("transform", Transform.TEXT),
                    vocabulary=value.get("vocabulary", {}),
                    literal=value.get("literal"),
                )
            )
        else:
            raise ValueError(
                f"{context}.{canonical}: expected a column name or a table, "
                f"got {type(value).__name__}"
            )
    return tuple(out)


def parse_mapping(document: Mapping[str, Any]) -> SourceMapping:
    """Build and validate a :class:`SourceMapping` from a parsed TOML document."""
    try:
        source_system = document["source_system"]
    except KeyError:
        raise ValueError("mapping is missing 'source_system'") from None

    policy_fields = _field_mappings(document.get("policy", {}), "policy")
    _validate_canonical([f.canonical for f in policy_fields], POLICY, "policy")

    parties: list[PartyMapping] = []
    for i, block in enumerate(document.get("party", [])):
        context = f"party[{i}]"
        for required in ("role", "key_field", "key_kind"):
            if required not in block:
                raise ValueError(f"{context}: missing {required!r}")
        try:
            role = PartyRole(block["role"])
        except ValueError:
            raise ValueError(
                f"{context}: {block['role']!r} is not a PartyRole; "
                f"valid: {[r.value for r in PartyRole]}"
            ) from None

        fields = _field_mappings(block.get("fields", {}), f"{context}.fields")
        _validate_canonical([f.canonical for f in fields], PERSON, f"{context}.fields")

        parties.append(
            PartyMapping(
                role=role,
                key_field=block["key_field"],
                key_kind=block["key_kind"],
                fields=fields,
                role_sequence=block.get("role_sequence", 1),
            )
        )

    if not parties:
        raise ValueError(f"{source_system}: no party blocks; nothing to resolve")

    return SourceMapping(
        source_system=source_system,
        policy=policy_fields,
        parties=tuple(parties),
        date_formats=tuple(document.get("date_formats", ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"))),
        default_country_code=str(document.get("default_country_code", "1")),
        source_timestamp_field=document.get("source_timestamp_field"),
    )


def load_mapping(path: str | Path) -> SourceMapping:
    """Read and validate a mapping file."""
    p = Path(path)
    with p.open("rb") as fh:
        document = tomllib.load(fh)
    try:
        return parse_mapping(document)
    except ValueError as exc:
        raise ValueError(f"{p}: {exc}") from None
