"""Tests for the canonical data model.

These check invariants that would otherwise fail late and expensively: schema
drift between the registry and the committed DDL, survivorship rules that
contradict their column types, and identifier properties the storage layout
depends on.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys
import uuid

import pytest

from cmdm.model import ENTITIES, PERSON, POLICY, RELATIONSHIP
from cmdm.model.control import ANCHORS, CONTROL_TABLES
from cmdm.model.ddl import render_schema, sql_type
from cmdm.model.enums import LogicalType as LT
from cmdm.model.enums import MatchRole as MR
from cmdm.model.enums import PiiClass as PII
from cmdm.model.enums import SurvivorshipStrategy as SS
from cmdm.model.fields import EntitySpec, FieldSpec
from cmdm.model.ids import (
    blocking_hash,
    keyed_identifier_hash,
    payload_hash,
    record_hash,
    uuid7,
    uuid7_at,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
COMMITTED_DDL = REPO / "src" / "cmdm" / "sql" / "001_golden_schema.sql"

ALL_SPECS = [*ANCHORS.values(), *ENTITIES.values(), *CONTROL_TABLES.values()]


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_three_canonical_entities() -> None:
    """The canonical model is exactly Policy, Person and Relationship."""
    assert set(ENTITIES) == {"Policy", "Person", "Relationship"}


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_field_names_are_unique_and_snake_case(spec: EntitySpec) -> None:
    names = [f.name for f in spec.fields]
    assert len(names) == len(set(names))
    for name in names:
        assert name == name.lower(), f"{spec.name}.{name} is not lower case"
        assert " " not in name and "-" not in name


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_primary_key_is_non_nullable_uuid(spec: EntitySpec) -> None:
    pk = spec.by_name[spec.primary_key]
    assert pk.dtype is LT.UUID
    assert not pk.nullable


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_derived_fields_never_claim_source_survivorship(spec: EntitySpec) -> None:
    """A derived value cannot be won from a source; it is computed.

    Enforced in FieldSpec.__post_init__ too. Asserted here so that the whole
    registry is covered rather than only the paths construction happens to take.
    """
    for f in spec.select(derived=True):
        assert f.survivorship in (SS.DERIVED, SS.SYSTEM), f"{spec.name}.{f.name}"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_lineage_columns_excluded_from_record_hash(spec: EntitySpec) -> None:
    """Audit columns must not feed change detection.

    If updated_at were hashed, every re-ingest of unchanged data would look like
    a change and manufacture a new SCD-2 version, which would make the version
    history meaningless and grow the table without bound.
    """
    for name in ("version", "valid_from", "valid_to", "is_current", "updated_at", "record_hash"):
        f = spec.by_name.get(name)
        if f is not None:
            assert not f.in_record_hash, f"{spec.name}.{name} must not be hashed"


def test_versioned_entities_carry_full_lineage() -> None:
    required = {
        "version", "valid_from", "valid_to", "is_current", "record_hash",
        "source_count", "confidence", "is_curated", "is_deleted",
        "created_at", "updated_at",
    }
    for spec in ENTITIES.values():
        assert required <= set(spec.by_name), f"{spec.name} missing {required - set(spec.by_name)}"


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------


def test_person_has_blocking_keys_across_independent_signals() -> None:
    """Blocking must not depend on a single attribute family.

    Blocking only on name loses anyone whose name was re-keyed; blocking only on
    contact details loses everyone the carrier holds no email for. The recall of
    the whole resolver is bounded by the union of these keys, so the model
    requires name, contact and address keys to all exist.
    """
    blocking = {f.name for f in PERSON.select(match_role=MR.BLOCKING)}
    assert any("name" in n for n in blocking)
    assert {"email_normalized", "phone_e164"} & blocking
    assert "address_key" in blocking


def test_name_derived_columns_do_not_replace_the_raw_name() -> None:
    """The source name is only ever supplied as one string; it must survive.

    Every parsed component is a guess. Losing the original would make a bad
    parse unrecoverable.
    """
    assert not PERSON.by_name["full_name"].derived
    assert not PERSON.by_name["full_name"].nullable
    for name in ("given_name_derived", "surname_derived", "full_name_normalized",
                 "name_sorted_key", "name_phonetic_key"):
        assert PERSON.by_name[name].derived, name


def test_parsed_name_components_carry_confidence() -> None:
    """A comparator must be able to discount a doubtful split."""
    assert "name_parse_confidence" in PERSON.by_name
    assert "name_parse_method" in PERSON.by_name


def test_national_id_is_never_stored_in_the_clear() -> None:
    """Only a keyed hash and the last four characters reach the golden record."""
    assert "national_id" not in PERSON.by_name
    assert PERSON.by_name["national_id_hash"].pii is PII.SENSITIVE
    assert PERSON.by_name["national_id_last4"].pii is PII.SENSITIVE


def test_suppression_flags_use_any_true_survivorship() -> None:
    """A suppression asserted by one source must not be outvoted.

    Losing an opt-out or a deceased flag in a merge is a compliance breach, not
    a data-quality blemish, so these fields cannot use a majority rule.
    """
    for name in ("do_not_contact", "is_deceased", "is_sanctioned"):
        assert PERSON.by_name[name].survivorship is SS.ANY_TRUE, name


def test_money_is_never_a_float() -> None:
    """Premiums are summed and reconciled against carrier ledgers."""
    for spec in ALL_SPECS:
        for f in spec.fields:
            if f.name.endswith("_amount"):
                assert f.dtype is LT.MONEY, f"{spec.name}.{f.name}"


# ---------------------------------------------------------------------------
# Relationship semantics
# ---------------------------------------------------------------------------


def test_relationship_supports_the_three_sourced_roles() -> None:
    from cmdm.model.enums import PartyRole

    assert {"OWNER", "INSURED", "AGENT"} <= {r.value for r in PartyRole}


def test_relationship_edge_targets_are_nullable_for_the_xor() -> None:
    """Exactly one target is populated per edge, so both must be nullable."""
    assert RELATIONSHIP.by_name["to_policy_id"].nullable
    assert RELATIONSHIP.by_name["to_person_id"].nullable
    assert not RELATIONSHIP.by_name["from_person_id"].nullable


def test_derived_edges_retain_their_evidence() -> None:
    """An inferred link must be traceable to the policies that produced it."""
    assert RELATIONSHIP.by_name["evidence_policy_ids"].dtype is LT.LIST_UUID
    assert RELATIONSHIP.by_name["derivation_method"].enum_name == "DerivationMethod"


def test_relationship_separates_system_time_from_real_world_time() -> None:
    """valid_from is when the system learned it; effective_from is when it began.

    Collapsing the two would make a backdated agent-of-record change
    unrepresentable.
    """
    assert {"valid_from", "valid_to", "effective_from", "effective_to"} <= set(
        RELATIONSHIP.by_name
    )


def test_source_party_key_survives_on_the_edge() -> None:
    """The exact assertion the source made must outlive a merge moving person_id."""
    f = RELATIONSHIP.by_name["source_party_key"]
    assert f.match_role is MR.IDENTIFIER
    assert "source_key_kind" in RELATIONSHIP.by_name


# ---------------------------------------------------------------------------
# DDL projection
# ---------------------------------------------------------------------------


def test_committed_ddl_matches_the_registry() -> None:
    """The generated schema is committed; drift fails the build.

    This is the guard that lets the registry be the single source of truth. Add
    a field without regenerating and this test says so.
    """
    assert COMMITTED_DDL.exists(), "run: python -m scripts.render_ddl"
    assert COMMITTED_DDL.read_text(encoding="utf-8") == render_schema(), (
        "DDL is stale; regenerate with: python -m scripts.render_ddl"
    )


def test_ddl_parses_with_the_real_postgres_parser() -> None:
    pglast = pytest.importorskip("pglast")
    statements = pglast.parse_sql(COMMITTED_DDL.read_text(encoding="utf-8"))
    assert len(statements) > 100


def test_ddl_enforces_one_current_version_per_entity() -> None:
    sql = COMMITTED_DDL.read_text(encoding="utf-8")
    for spec in ENTITIES.values():
        assert f"CREATE UNIQUE INDEX uq_{spec.table}_current" in sql, spec.table


def test_ddl_enforces_the_relationship_xor() -> None:
    sql = COMMITTED_DDL.read_text(encoding="utf-8")
    assert "ck_relationship_target" in sql
    assert "ck_relationship_role" in sql


def test_ddl_foreign_keys_target_anchor_tables_only() -> None:
    """A versioned table cannot back a foreign key.

    Its surrogate key repeats once per version, and Postgres will not accept a
    partial unique index as a reference target. Every FK must therefore point at
    an anchor.
    """
    sql = COMMITTED_DDL.read_text(encoding="utf-8")
    for line in sql.splitlines():
        if "REFERENCES" in line:
            assert "_master" in line or "source_record" in line, line


def test_enum_fields_map_to_postgres_enum_types() -> None:
    assert sql_type(POLICY.by_name["policy_status"]) == "policy_status"
    assert sql_type(PERSON.by_name["party_type"]) == "party_type"


def test_render_ddl_script_is_idempotent() -> None:
    """Running the generator twice produces identical output."""
    assert render_schema() == render_schema()


def test_render_ddl_script_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.render_ddl"],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Arrow projection
# ---------------------------------------------------------------------------


def test_arrow_schema_covers_every_field() -> None:
    pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    for spec in ALL_SPECS:
        schema = arrow_schema(spec)
        assert [f.name for f in schema] == [f.name for f in spec.fields], spec.name


def test_arrow_money_is_decimal_not_float() -> None:
    pa = pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    field = arrow_schema(POLICY).field("annual_premium_amount")
    assert pa.types.is_decimal(field.type)


def test_arrow_enums_are_dictionary_encoded() -> None:
    pa = pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    assert pa.types.is_dictionary(arrow_schema(POLICY).field("policy_status").type)


def test_arrow_name_tokens_is_a_real_list() -> None:
    """Token-set similarity must be an array kernel, not a re-split per pair."""
    pa = pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    assert pa.types.is_large_list(arrow_schema(PERSON).field("name_tokens").type)


def test_arrow_metadata_carries_match_and_pii_roles() -> None:
    """Metadata rides with the batch so downstream workers need no config."""
    pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    field = arrow_schema(PERSON).field("name_phonetic_key")
    assert field.metadata[b"match_role"] == b"BLOCKING"
    assert field.metadata[b"pii"] == b"DIRECT"


def test_staging_schema_omits_lineage_columns() -> None:
    pytest.importorskip("pyarrow")
    from cmdm.model.arrow import arrow_schema

    staging = {f.name for f in arrow_schema(PERSON, include_lineage=False)}
    assert "full_name" in staging
    assert "valid_from" not in staging
    assert "is_current" not in staging


# ---------------------------------------------------------------------------
# Identifiers and hashing
# ---------------------------------------------------------------------------


def test_uuid7_is_version_7_and_correct_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_sorts_by_creation_time() -> None:
    """Time ordering is why these are used instead of uuid4.

    Random keys scatter B-tree inserts across the index and turn every bulk load
    into a random-write workload; time-ordered keys append.
    """
    earlier = uuid7_at(1_600_000_000_000)
    later = uuid7_at(1_700_000_000_000)
    assert earlier < later
    assert str(earlier) < str(later)


def test_uuid7_embeds_the_supplied_timestamp() -> None:
    ms = 1_700_000_000_123
    assert (uuid7_at(ms).int >> 80) == ms


def test_uuid7_rejects_out_of_range_timestamps() -> None:
    with pytest.raises(ValueError):
        uuid7_at(-1)
    with pytest.raises(ValueError):
        uuid7_at(1 << 48)


def test_uuid7_values_are_distinct_within_a_millisecond() -> None:
    values = {uuid7_at(1_700_000_000_000) for _ in range(1000)}
    assert len(values) == 1000


def test_record_hash_is_stable_and_order_independent() -> None:
    fields = ["a", "b"]
    assert record_hash({"a": 1, "b": "x"}, fields) == record_hash({"b": "x", "a": 1}, fields)


def test_record_hash_distinguishes_null_from_empty_string() -> None:
    """Unknown and known-to-be-blank are different facts in this model."""
    assert record_hash({"a": None}, ["a"]) != record_hash({"a": ""}, ["a"])


def test_record_hash_ignores_fields_outside_the_hash_set() -> None:
    """Change detection must not react to audit columns."""
    base = {"full_name": "JOHN SMITH", "updated_at": "2024-01-01"}
    changed = {"full_name": "JOHN SMITH", "updated_at": "2025-06-01"}
    assert record_hash(base, ["full_name"]) == record_hash(changed, ["full_name"])


def test_record_hash_detects_a_real_change() -> None:
    a = record_hash({"full_name": "JOHN SMITH"}, ["full_name"])
    b = record_hash({"full_name": "JOHN SMYTH"}, ["full_name"])
    assert a != b


def test_record_hash_is_not_confusable_across_field_boundaries() -> None:
    """Hashing ('ab','c') must not collide with ('a','bc')."""
    left = record_hash({"x": "ab", "y": "c"}, ["x", "y"])
    right = record_hash({"x": "a", "y": "bc"}, ["x", "y"])
    assert left != right


def test_record_hash_handles_dates_and_nested_values() -> None:
    values = {
        "d": dt.date(1980, 5, 1),
        "t": dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        "tokens": ["JOHN", "SMITH"],
        "meta": {"b": 2, "a": 1},
    }
    first = record_hash(values, sorted(values))
    assert first == record_hash(values, sorted(values))


def test_payload_hash_detects_redelivery() -> None:
    payload = {"PolicyNumber": "POL-1", "OwnerName": "JOHN SMITH"}
    assert payload_hash(payload) == payload_hash(dict(reversed(list(payload.items()))))
    assert payload_hash(payload) != payload_hash({**payload, "OwnerName": "JANE SMITH"})


def test_keyed_identifier_hash_requires_a_key(monkeypatch) -> None:
    """An unkeyed digest of a nine-digit identifier is trivially reversible.

    The variable is removed rather than assumed absent. A deployment that has
    exported it -- which is every real one, and the offline bundle's verifier --
    would otherwise see this test fail for the one reason that is not a defect.
    """
    monkeypatch.delenv("CMDM_ID_HASH_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CMDM_ID_HASH_KEY"):
        keyed_identifier_hash("123-45-6789", id_type="SSN", key=None)


def test_keyed_identifier_hash_normalizes_punctuation() -> None:
    key = b"test-key"
    assert keyed_identifier_hash("123-45-6789", id_type="SSN", key=key) == keyed_identifier_hash(
        "123456789", id_type="SSN", key=key
    )


def test_keyed_identifier_hash_separates_identifier_types() -> None:
    """The same digits as an SSN and as a passport must not collide."""
    key = b"test-key"
    assert keyed_identifier_hash("123456789", id_type="SSN", key=key) != keyed_identifier_hash(
        "123456789", id_type="PASSPORT", key=key
    )


def test_keyed_identifier_hash_depends_on_the_key() -> None:
    assert keyed_identifier_hash("123456789", id_type="SSN", key=b"a") != keyed_identifier_hash(
        "123456789", id_type="SSN", key=b"b"
    )


def test_keyed_identifier_hash_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        keyed_identifier_hash("---", id_type="SSN", key=b"k")


def test_blocking_hash_is_stable_and_discriminating() -> None:
    assert blocking_hash("1 HIGH ST", "2000") == blocking_hash("1 HIGH ST", "2000")
    assert blocking_hash("1 HIGH ST", "2000") != blocking_hash("1 HIGH ST", "2001")
    assert blocking_hash("1 HIGH ST", None) != blocking_hash("1 HIGH ST", "")


# ---------------------------------------------------------------------------
# FieldSpec validation
# ---------------------------------------------------------------------------


def test_field_spec_rejects_derived_with_source_survivorship() -> None:
    with pytest.raises(ValueError, match="derived"):
        FieldSpec("x", LT.STRING, "doc", derived=True, survivorship=SS.MOST_RECENT)


def test_field_spec_rejects_non_string_enum() -> None:
    with pytest.raises(ValueError, match="enum-backed"):
        FieldSpec("x", LT.INT32, "doc", enum_name="PolicyStatus")


def test_entity_spec_rejects_unknown_primary_key() -> None:
    with pytest.raises(ValueError, match="primary key"):
        EntitySpec("X", "x", "missing", "doc", [FieldSpec("a", LT.STRING, "doc")])


def test_entity_spec_rejects_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        EntitySpec(
            "X", "x", "a", "doc",
            [FieldSpec("a", LT.UUID, "doc", nullable=False), FieldSpec("a", LT.STRING, "doc")],
        )


def test_every_field_is_documented() -> None:
    """A canonical model whose fields are undocumented is not canonical."""
    for spec in ALL_SPECS:
        for f in spec.fields:
            assert len(f.doc) > 20, f"{spec.name}.{f.name} needs a real description"
