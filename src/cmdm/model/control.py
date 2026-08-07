"""Control tables: the plumbing that makes the three entities defensible.

The canonical model has three entities. It cannot function with only three
tables. These five carry the evidence, the identity crosswalk and the audit
trail that turn "here is a golden record" into "here is a golden record, here is
every source value behind it, here is why each value won, and here is who or
what decided".

They are deliberately kept out of :mod:`cmdm.model.fields` so that the entity
registry stays exactly the three entities specified, while still being declared
with the same :class:`~cmdm.model.fields.FieldSpec` machinery so that the DDL and
Arrow projections cover them without a second code path.

*   ``source_record`` - immutable landing zone. Every inbound policy row, stored
    verbatim and content-addressed, before any interpretation.
*   ``person_xref`` - the identity crosswalk. The only place that knows a source
    party identifier maps to a golden person.
*   ``policy_xref`` - the same for policies, used after book transfers.
*   ``attribute_provenance`` - per-attribute survivorship record: which source
    won this field on this version, and why.
*   ``match_audit`` - append-only log of every resolution decision, including
    the ones the AI fallback made and the ones a steward overturned.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from cmdm.model.enums import LogicalType as LT
from cmdm.model.enums import MatchRole as MR
from cmdm.model.enums import PiiClass as PII
from cmdm.model.enums import SurvivorshipStrategy as SS
from cmdm.model.fields import EntitySpec, FieldSpec

__all__ = [
    "PERSON_MASTER",
    "POLICY_MASTER",
    "SOURCE_RECORD",
    "PERSON_XREF",
    "POLICY_XREF",
    "ATTRIBUTE_PROVENANCE",
    "MATCH_AUDIT",
    "ANCHORS",
    "CONTROL_TABLES",
]


_SYS = dict(survivorship=SS.SYSTEM)
_DER = dict(survivorship=SS.SYSTEM, derived=True)


def _anchor(entity: str, table: str, pk: str, extra_doc: str) -> EntitySpec:
    """Build an identity anchor table for a versioned entity.

    The entity tables are SCD-2, so a surrogate key appears on many rows and is
    not unique there. That makes it useless as a foreign key target: Postgres
    requires a full unique constraint, and a partial unique index over current
    versions does not qualify.

    The anchor solves this with one row per entity id, holding nothing but the
    identity itself. Versions reference it, the crosswalk references it, and
    edges reference it, so referential integrity is enforced by the database
    instead of being left to the writers and a nightly reconciliation job. It
    also gives a merged-away id somewhere to keep existing, which is what stops
    previously published ids from dangling.
    """
    return EntitySpec(
        name=entity,
        table=table,
        primary_key=pk,
        doc=f"Identity anchor for {table.removesuffix('_master')}. {extra_doc}",
        fields=[
            FieldSpec(pk, LT.UUID, "The surrogate identity itself. One row per entity, "
                      "created once and never reused.", nullable=False, indexed=True, **_DER),
            FieldSpec("created_at", LT.TIMESTAMP_TZ, "When the identity was minted.",
                      nullable=False, **_DER),
            FieldSpec("is_active", LT.BOOL,
                      "False once the identity has been merged away. The row stays so "
                      "that ids already published to consumers keep resolving.",
                      nullable=False, indexed=True, **_DER),
            FieldSpec("merged_into_id", LT.UUID,
                      "Winning identity, when this one lost a merge. Null otherwise. "
                      "Following this pointer is how a retired id resolves to the "
                      "surviving record.", indexed=True, **_SYS),
        ],
    )


PERSON_MASTER = _anchor(
    "PersonMaster",
    "person_master",
    "person_id",
    "Every person_id ever issued, including those retired by a merge.",
)

POLICY_MASTER = _anchor(
    "PolicyMaster",
    "policy_master",
    "policy_id",
    "Every policy_id ever issued, including those retired by a cross-source "
    "consolidation.",
)


SOURCE_RECORD = EntitySpec(
    name="SourceRecord",
    table="source_record",
    primary_key="source_record_id",
    natural_key=("source_system", "source_batch_id", "source_row_key"),
    doc=(
        "Immutable landing zone. One row per inbound policy record, exactly as "
        "received.\n\n"
        "Nothing in this table is ever updated or interpreted. It exists so that "
        "the entire golden layer can be dropped and rebuilt deterministically, "
        "and so that any disputed golden value can be traced to the literal bytes "
        "that produced it. Content addressing by payload hash makes re-delivery "
        "of an unchanged file a no-op rather than a duplicate."
    ),
    fields=[
        FieldSpec("source_record_id", LT.UUID, "Surrogate key for the landed row.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("source_system", LT.STRING, "System the record came from.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_batch_id", LT.STRING, "Ingestion batch or file identifier.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_row_key", LT.STRING,
                  "Natural key of the row within its batch, normally the policy "
                  "number.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("payload_hash", LT.STRING,
                  "BLAKE2b digest of the canonicalized payload. Re-delivery of an "
                  "identical row is recognized and skipped on this column.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("payload", LT.JSON,
                  "The record as received, with original field names and values. "
                  "Stored as JSONB so that a source schema change lands without a "
                  "migration and stays queryable.",
                  nullable=False, pii=PII.DIRECT, **_SYS),
        FieldSpec("source_timestamp", LT.TIMESTAMP_TZ,
                  "When the source asserts this record was last changed. Drives "
                  "MOST_RECENT survivorship; falls back to ingest time when the "
                  "source omits it, which is itself recorded as a quality flag.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("ingested_at", LT.TIMESTAMP_TZ, "When this system received the record.",
                  nullable=False, **_DER),
        FieldSpec("is_processed", LT.BOOL,
                  "Whether the record has been projected into the golden layer.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("reject_reason", LT.STRING,
                  "Why the record could not be projected. Rejects stay in place "
                  "rather than being discarded, so a bad feed is diagnosable.",
                  **_DER),
    ],
)


PERSON_XREF = EntitySpec(
    name="PersonXref",
    table="person_xref",
    primary_key="xref_id",
    natural_key=("source_system", "source_key_kind", "source_party_key"),
    doc=(
        "The identity crosswalk, and the answer to how the three source party "
        "identifiers become one golden person.\n\n"
        "Every (source_system, source_key_kind, source_party_key) triple resolves "
        "to exactly one person_id; many triples resolve to the same person. That "
        "is what lets an OwnerCustomerId from one platform and an "
        "InsuredCustomerId from another collapse into a single party once "
        "matching establishes they are the same human, without either source "
        "identifier being lost or rewritten.\n\n"
        "Retired person_ids also live here. When two persons merge, the loser's "
        "id is inserted as a RETIRED_ID row pointing at the winner, so ids handed "
        "out to downstream consumers keep resolving after the merge."
    ),
    fields=[
        FieldSpec("xref_id", LT.UUID, "Surrogate key for this crosswalk entry.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("person_id", LT.UUID, "Golden person this key currently resolves to.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_system", LT.STRING, "System that issued the key.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_key_kind", LT.STRING,
                  "Identifier namespace: OWNER_CUSTOMER_ID, INSURED_CUSTOMER_ID, "
                  "AGENT_CODE or RETIRED_ID. Part of the key because the same "
                  "literal can be valid in more than one namespace.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_party_key", LT.STRING, "The identifier value itself.",
                  nullable=False, match_role=MR.IDENTIFIER, pii=PII.INDIRECT,
                  indexed=True, **_SYS),
        FieldSpec("is_active", LT.BOOL,
                  "False once the mapping has been superseded by a merge or split. "
                  "Rows are never deleted, so the crosswalk's own history is intact.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("linked_at", LT.TIMESTAMP_TZ, "When this mapping was established.",
                  nullable=False, **_DER),
        FieldSpec("linked_by", LT.STRING,
                  "Process or steward that established it.", nullable=False, **_SYS),
        FieldSpec("derivation_method", LT.STRING,
                  "How the link was decided, from exact key match to AI fallback.",
                  nullable=False, enum_name="DerivationMethod", indexed=True, **_SYS),
        FieldSpec("confidence", LT.RATIO, "Confidence in this specific link.",
                  nullable=False, **_SYS),
    ],
)


POLICY_XREF = EntitySpec(
    name="PolicyXref",
    table="policy_xref",
    primary_key="xref_id",
    natural_key=("source_system", "source_policy_key"),
    doc=(
        "Policy-side crosswalk. Policy numbers are unique within a source, so this "
        "is near-trivial in steady state, but it earns its place after a book "
        "transfer or platform migration, when the same contract acquires a second "
        "policy number under a new administrator and both must keep resolving."
    ),
    fields=[
        FieldSpec("xref_id", LT.UUID, "Surrogate key for this crosswalk entry.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("policy_id", LT.UUID, "Golden policy this key resolves to.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_system", LT.STRING, "System that issued the policy number.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("source_policy_key", LT.STRING, "Normalized source policy number.",
                  nullable=False, match_role=MR.IDENTIFIER, indexed=True, **_SYS),
        FieldSpec("is_active", LT.BOOL, "False once superseded.", nullable=False, **_DER),
        FieldSpec("linked_at", LT.TIMESTAMP_TZ, "When the mapping was established.",
                  nullable=False, **_DER),
        FieldSpec("derivation_method", LT.STRING, "How the link was decided.",
                  nullable=False, enum_name="DerivationMethod", **_SYS),
    ],
)


ATTRIBUTE_PROVENANCE = EntitySpec(
    name="AttributeProvenance",
    table="attribute_provenance",
    primary_key="provenance_id",
    doc=(
        "Per-attribute survivorship record: for one field on one version of one "
        "golden record, which source contributed the winning value and under "
        "which rule.\n\n"
        "This is the table that answers the only question stewards actually ask, "
        "which is not 'what is the golden value' but 'why is it that and not the "
        "other one'. It is narrow and tall by design so that it stays cheap to "
        "write in bulk from a vectorized survivorship pass, and it stores the "
        "losing candidates alongside the winner so a disputed field can be "
        "re-adjudicated without re-reading the source layer."
    ),
    fields=[
        FieldSpec("provenance_id", LT.UUID, "Surrogate key for this survivorship decision.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("entity_name", LT.STRING, "Policy, Person or Relationship.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("entity_id", LT.UUID, "Golden id of the record.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("entity_version", LT.INT32, "Version of the record this applies to.",
                  nullable=False, **_SYS),
        FieldSpec("attribute_name", LT.STRING, "Canonical field name.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("winning_source_record_id", LT.UUID,
                  "Source record the surviving value came from.", nullable=False, **_SYS),
        FieldSpec("winning_source_system", LT.STRING,
                  "Source system the winning record came from, denormalized so that "
                  "provenance reads need no join back to the landing zone.",
                  nullable=False, **_SYS),
        FieldSpec("strategy", LT.STRING, "Survivorship rule that selected it.",
                  nullable=False, enum_name="SurvivorshipStrategy", **_SYS),
        FieldSpec("value_text", LT.STRING,
                  "The winning value rendered as text. Denormalized deliberately: "
                  "one column beats one per type, and this table is read by "
                  "humans and diff tools, not by the query planner.",
                  pii=PII.DIRECT, **_SYS),
        FieldSpec("candidate_count", LT.INT32,
                  "How many distinct non-null candidates were considered.",
                  nullable=False, **_SYS),
        FieldSpec("rejected_values", LT.JSON,
                  "The losing candidates with their sources, so a contested field "
                  "can be re-adjudicated without going back to the landing zone.",
                  pii=PII.DIRECT, **_SYS),
        FieldSpec("decided_at", LT.TIMESTAMP_TZ, "When survivorship ran.",
                  nullable=False, **_DER),
    ],
)


MATCH_AUDIT = EntitySpec(
    name="MatchAudit",
    table="match_audit",
    primary_key="audit_id",
    doc=(
        "Append-only log of every entity-resolution decision, including "
        "non-matches.\n\n"
        "Logging the negatives matters as much as the positives: a duplicate that "
        "reaches production is investigated by asking why the pair was compared "
        "and rejected, which is unanswerable if only merges are recorded.\n\n"
        "This is also where the AI fallback is held accountable. Rows with "
        "derivation_method AI_FALLBACK carry the model identifier, the prompt "
        "hash and the raw model output, so every decision a local model made is "
        "reproducible, reviewable in isolation, and revocable in bulk if the "
        "model turns out to be wrong."
    ),
    fields=[
        FieldSpec("audit_id", LT.UUID, "Surrogate key for this resolution decision.",
                  nullable=False, indexed=True, **_DER),
        FieldSpec("entity_name", LT.STRING, "Entity the decision concerns.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("left_id", LT.UUID, "One side of the compared pair.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("right_id", LT.UUID, "The other side of the compared pair.",
                  nullable=False, indexed=True, **_SYS),
        FieldSpec("decision", LT.STRING, "Match, no-match, review or rule-blocked.",
                  nullable=False, enum_name="MatchDecision", indexed=True, **_SYS),
        FieldSpec("score", LT.RATIO, "Composite similarity score for the pair.", **_SYS),
        FieldSpec("blocking_key", LT.STRING, "Blocking key that brought the pair together. "
                  "Tuning recall means knowing which keys produce which pairs.",
                  indexed=True, **_SYS),
        FieldSpec("comparator_scores", LT.JSON,
                  "Per-comparator contributions. Makes a composite score "
                  "explainable rather than merely reportable.", **_SYS),
        FieldSpec("derivation_method", LT.STRING, "Which engine decided.",
                  nullable=False, enum_name="DerivationMethod", indexed=True, **_SYS),
        FieldSpec("model_name", LT.STRING,
                  "Local model identifier, when the AI fallback was invoked.", **_SYS),
        FieldSpec("model_version", LT.STRING, "Model version or digest.", **_SYS),
        FieldSpec("prompt_hash", LT.STRING,
                  "Digest of the rendered prompt, so a decision can be replayed "
                  "against the exact input that produced it.", **_SYS),
        FieldSpec("model_output", LT.JSON,
                  "Raw structured output from the model, retained verbatim.", **_SYS),
        FieldSpec("resulting_person_id", LT.UUID,
                  "Golden id the pair collapsed into, when the decision was a match.",
                  indexed=True, **_SYS),
        FieldSpec("reviewed_by", LT.STRING,
                  "Steward who confirmed or overturned the decision.", **_SYS),
        FieldSpec("review_outcome", LT.STRING,
                  "What the steward concluded. Disagreements between this and "
                  "decision are the labelled training set for tuning thresholds.",
                  enum_name="MatchDecision", **_SYS),
        FieldSpec("decided_at", LT.TIMESTAMP_TZ, "When the decision was made.",
                  nullable=False, indexed=True, **_DER),
    ],
)


#: Anchor tables. Created before the entity tables, which reference them.
ANCHORS: Mapping[str, EntitySpec] = MappingProxyType(
    {e.name: e for e in (PERSON_MASTER, POLICY_MASTER)}
)

CONTROL_TABLES: Mapping[str, EntitySpec] = MappingProxyType(
    {
        e.name: e
        for e in (SOURCE_RECORD, PERSON_XREF, POLICY_XREF, ATTRIBUTE_PROVENANCE, MATCH_AUDIT)
    }
)
