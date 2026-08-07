"""Postgres DDL projection of the canonical model.

Generates the golden-store schema from the registry. The output is committed to
``src/cmdm/sql/001_golden_schema.sql`` and a test regenerates it and compares, so
a field added to the registry without regenerating the DDL fails the build
rather than drifting quietly.

The generated schema does real work beyond declaring columns:

*   **One current version per entity is a database constraint.** A partial
    unique index on ``(entity_id) WHERE is_current`` makes it impossible for a
    buggy writer to leave two current versions of a person, rather than making
    it merely unlikely.
*   **The Relationship XOR is a check constraint.** An edge points at a policy
    or at a person, never both and never neither, enforced by the database.
*   **Enums are Postgres enum types.** One byte-ish on disk, and an unmapped
    vocabulary value is rejected at write time instead of accumulating.
*   **Blocking keys are indexed.** They are probed once per candidate-generation
    pass over the whole population, which is the hottest read in the system.

Postgres is the recommended golden store because the requirement is ACID
golden-record storage and merges are multi-row transactions: closing one
person's current version, opening another's, rewriting the crosswalk and
repointing the edges must all commit or none of it. DuckDB is used alongside it
for the analytical and matching passes, reading the same Parquet the vectorized
layer writes; it is not the system of record.
"""

from __future__ import annotations

from collections.abc import Iterable

from cmdm.model.control import ANCHORS, CONTROL_TABLES
from cmdm.model.enums import (
    AssociationType,
    DerivationMethod,
    EdgeKind,
    Gender,
    MaritalStatus,
    MatchDecision,
    NameParseMethod,
    NationalIdType,
    PartyRole,
    PartyType,
    PolicyStatus,
    PremiumFrequency,
    ProductLine,
    SurvivorshipStrategy,
)
from cmdm.model.enums import LogicalType as LT
from cmdm.model.fields import (
    ENTITIES,
    MONEY_PRECISION,
    MONEY_SCALE,
    EntitySpec,
    FieldSpec,
)

__all__ = ["sql_type", "render_table", "render_schema", "SCHEMA_NAME"]

SCHEMA_NAME = "mdm"

#: Enum name -> the enum class, so the DDL can emit CREATE TYPE for each.
_ENUM_TYPES = {
    "PartyType": PartyType,
    "PartyRole": PartyRole,
    "AssociationType": AssociationType,
    "EdgeKind": EdgeKind,
    "PolicyStatus": PolicyStatus,
    "ProductLine": ProductLine,
    "PremiumFrequency": PremiumFrequency,
    "Gender": Gender,
    "MaritalStatus": MaritalStatus,
    "NationalIdType": NationalIdType,
    "NameParseMethod": NameParseMethod,
    "DerivationMethod": DerivationMethod,
    "MatchDecision": MatchDecision,
    "SurvivorshipStrategy": SurvivorshipStrategy,
}

_SQL_TYPES = {
    LT.STRING: "text",
    LT.BOOL: "boolean",
    LT.INT16: "smallint",
    LT.INT32: "integer",
    LT.INT64: "bigint",
    LT.FLOAT32: "real",
    LT.FLOAT64: "double precision",
    LT.RATIO: "double precision",
    LT.MONEY: f"numeric({MONEY_PRECISION}, {MONEY_SCALE})",
    LT.DATE: "date",
    LT.TIMESTAMP_TZ: "timestamptz",
    LT.UUID: "uuid",
    LT.LIST_STRING: "text[]",
    LT.LIST_UUID: "uuid[]",
    LT.JSON: "jsonb",
}


def _enum_type_name(enum_name: str) -> str:
    """Postgres type name for an enum: PolicyStatus -> policy_status."""
    out: list[str] = []
    for i, ch in enumerate(enum_name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def sql_type(field: FieldSpec) -> str:
    """Postgres column type for a field spec."""
    if field.enum_name is not None:
        return _enum_type_name(field.enum_name)
    try:
        return _SQL_TYPES[field.dtype]
    except KeyError:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"no SQL mapping for {field.dtype}") from None


def _wrap_comment(text: str, width: int = 88) -> list[str]:
    """Wrap a docstring into SQL line comments, preserving paragraph breaks."""
    lines: list[str] = []
    for para in text.split("\n\n"):
        words = para.split()
        if not words:
            continue
        current = "--"
        for word in words:
            if len(current) + len(word) + 1 > width:
                lines.append(current)
                current = "--"
            current += " " + word
        lines.append(current)
        lines.append("--")
    if lines and lines[-1] == "--":
        lines.pop()
    return lines


def _is_versioned(spec: EntitySpec) -> bool:
    """True when the entity carries SCD-2 lineage columns."""
    return "is_current" in spec.by_name


def render_table(spec: EntitySpec) -> str:
    """Render CREATE TABLE plus indexes and constraints for one entity."""
    versioned = _is_versioned(spec)
    out: list[str] = []
    out.extend(_wrap_comment(f"{spec.name}. {spec.doc}"))
    out.append(f"CREATE TABLE {SCHEMA_NAME}.{spec.table} (")

    cols: list[str] = []
    if versioned:
        # The physical primary key is (entity_id, version): a versioned table
        # holds many rows per entity and only the pair is unique.
        cols.append("    row_id            bigint GENERATED ALWAYS AS IDENTITY")

    width = max(len(f.name) for f in spec.fields) + 2
    for f in spec.fields:
        null = "" if f.nullable else " NOT NULL"
        cols.append(f"    {f.name:<{width}}{sql_type(f)}{null}")

    if versioned:
        cols.append(f"    CONSTRAINT pk_{spec.table} PRIMARY KEY ({spec.primary_key}, version)")
    else:
        cols.append(f"    CONSTRAINT pk_{spec.table} PRIMARY KEY ({spec.primary_key})")

    if spec.table == "relationship":
        # An edge targets a policy or a party, never both and never neither.
        cols.append(
            "    CONSTRAINT ck_relationship_target CHECK (\n"
            "        (edge_kind = 'PARTY_POLICY' AND to_policy_id IS NOT NULL "
            "AND to_person_id IS NULL)\n"
            "        OR (edge_kind = 'PARTY_PARTY' AND to_person_id IS NOT NULL "
            "AND to_policy_id IS NULL)\n"
            "    )"
        )
        # A sourced role edge must say what the role is; a derived edge must say
        # what kind of association it asserts.
        cols.append(
            "    CONSTRAINT ck_relationship_role CHECK (\n"
            "        (edge_kind = 'PARTY_POLICY' AND role IS NOT NULL)\n"
            "        OR (edge_kind = 'PARTY_PARTY' AND association_type IS NOT NULL)\n"
            "    )"
        )
        cols.append(
            "    CONSTRAINT ck_relationship_no_self CHECK (\n"
            "        to_person_id IS DISTINCT FROM from_person_id\n"
            "    )"
        )

    if versioned:
        cols.append(
            f"    CONSTRAINT ck_{spec.table}_validity CHECK ("
            "valid_to IS NULL OR valid_to > valid_from)"
        )
        cols.append(
            f"    CONSTRAINT ck_{spec.table}_current CHECK (is_current = (valid_to IS NULL))"
        )

    out.append(",\n".join(cols))
    out.append(");")
    out.append("")

    # Column comments carry the field docs into the database, so psql \d+ and
    # every BI catalogue that reads pg_description explain themselves.
    for f in spec.fields:
        doc = " ".join(f.doc.split()).replace("'", "''")
        out.append(f"COMMENT ON COLUMN {SCHEMA_NAME}.{spec.table}.{f.name} IS '{doc}';")
    out.append("")

    if versioned:
        out.append("-- Exactly one current version per entity, enforced by the database")
        out.append("-- rather than by writer discipline.")
        out.append(
            f"CREATE UNIQUE INDEX uq_{spec.table}_current\n"
            f"    ON {SCHEMA_NAME}.{spec.table} ({spec.primary_key})\n"
            f"    WHERE is_current;"
        )
        out.append("")

    if spec.natural_key:
        keys = ", ".join(spec.natural_key)
        pred = "\n    WHERE is_current" if versioned else ""
        out.append("-- Natural key: deterministic identity within a source system.")
        out.append(
            f"CREATE UNIQUE INDEX uq_{spec.table}_natural\n"
            f"    ON {SCHEMA_NAME}.{spec.table} ({keys}){pred};"
        )
        out.append("")

    indexed = [f for f in spec.fields if f.indexed and f.name != spec.primary_key]
    if indexed:
        out.append("-- Blocking keys and lookup columns. These are probed once per")
        out.append("-- candidate-generation pass over the whole population.")
        for f in indexed:
            pred = "\n    WHERE is_current" if versioned else ""
            out.append(
                f"CREATE INDEX ix_{spec.table}_{f.name}\n"
                f"    ON {SCHEMA_NAME}.{spec.table} ({f.name}){pred};"
            )
        out.append("")

    return "\n".join(out)


def _render_enums() -> str:
    out = [
        "-- Controlled vocabularies. Declared as Postgres enums so an unmapped",
        "-- source value is rejected at write time rather than accumulating as",
        "-- free text nobody notices until a report is wrong.",
        "",
    ]
    for name, cls in _ENUM_TYPES.items():
        values = ",\n    ".join(f"'{m.value}'" for m in cls)
        out.append(f"CREATE TYPE {SCHEMA_NAME}.{_enum_type_name(name)} AS ENUM (\n    {values}\n);")
        out.append("")
    return "\n".join(out)


def render_schema(specs: Iterable[EntitySpec] | None = None) -> str:
    """Render the full golden-store schema.

    Order matters: enums before the tables that use them, entities before the
    control tables that reference their ids.
    """
    if specs is None:
        specs = [*ANCHORS.values(), *ENTITIES.values(), *CONTROL_TABLES.values()]

    header = f"""\
-- Customer MDM golden store: canonical schema.
--
-- GENERATED FILE. Do not edit by hand.
-- Regenerate with:  python -m scripts.render_ddl
-- The source of truth is src/cmdm/model/fields.py and src/cmdm/model/control.py;
-- a test regenerates this file and fails if it differs from what is committed.
--
-- Three canonical entities -- Policy, Person, Relationship -- plus the control
-- tables that hold the evidence, the identity crosswalk and the audit trail.
--
-- Golden records are versioned, never updated in place. A change closes the
-- current version by stamping valid_to and inserts a new row with an
-- incremented version, so "what did this record look like in March" stays
-- answerable and every merge is reversible.

CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};

SET search_path TO {SCHEMA_NAME}, public;
"""

    parts = [header, "", _render_enums()]
    for spec in specs:
        parts.append(render_table(spec))
    parts.append(_render_foreign_keys())
    return "\n".join(parts)


def _render_foreign_keys() -> str:
    """Referential integrity, declared after all tables exist.

    Every foreign key targets an anchor table rather than a versioned entity
    table. The entity tables are SCD-2, so their surrogate keys repeat across
    versions and are not unique; the partial unique index over current versions
    does not qualify as a foreign key target either, because Postgres requires a
    full unique constraint. The anchors carry exactly one row per identity,
    which makes every reference below enforceable by the database.
    """
    return """\
-- Referential integrity.
--
-- All references target the anchor tables, which hold one row per identity.
-- The versioned entity tables cannot be referenced directly: their surrogate
-- keys repeat once per version, and a partial unique index over current
-- versions is not a valid foreign key target in Postgres.

-- Versions belong to an identity.
ALTER TABLE mdm.person
    ADD CONSTRAINT fk_person_master
    FOREIGN KEY (person_id) REFERENCES mdm.person_master (person_id);

ALTER TABLE mdm.policy
    ADD CONSTRAINT fk_policy_master
    FOREIGN KEY (policy_id) REFERENCES mdm.policy_master (policy_id);

-- A retired identity points at the one that absorbed it.
ALTER TABLE mdm.person_master
    ADD CONSTRAINT fk_person_master_merged_into
    FOREIGN KEY (merged_into_id) REFERENCES mdm.person_master (person_id);

ALTER TABLE mdm.policy_master
    ADD CONSTRAINT fk_policy_master_merged_into
    FOREIGN KEY (merged_into_id) REFERENCES mdm.policy_master (policy_id);

-- Edges. Deferred, because a merge repoints every edge of the losing party in
-- the same transaction that retires it, and the intermediate state is
-- legitimately inconsistent until commit.
ALTER TABLE mdm.relationship
    ADD CONSTRAINT fk_relationship_from_person
    FOREIGN KEY (from_person_id) REFERENCES mdm.person_master (person_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE mdm.relationship
    ADD CONSTRAINT fk_relationship_to_person
    FOREIGN KEY (to_person_id) REFERENCES mdm.person_master (person_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE mdm.relationship
    ADD CONSTRAINT fk_relationship_to_policy
    FOREIGN KEY (to_policy_id) REFERENCES mdm.policy_master (policy_id)
    DEFERRABLE INITIALLY DEFERRED;

-- Crosswalk.
ALTER TABLE mdm.person_xref
    ADD CONSTRAINT fk_person_xref_person
    FOREIGN KEY (person_id) REFERENCES mdm.person_master (person_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE mdm.policy_xref
    ADD CONSTRAINT fk_policy_xref_policy
    FOREIGN KEY (policy_id) REFERENCES mdm.policy_master (policy_id)
    DEFERRABLE INITIALLY DEFERRED;

-- Provenance points back at the landed record that supplied the winning value,
-- so a golden attribute is always traceable to literal source bytes.
ALTER TABLE mdm.attribute_provenance
    ADD CONSTRAINT fk_provenance_source_record
    FOREIGN KEY (winning_source_record_id) REFERENCES mdm.source_record (source_record_id);
"""
