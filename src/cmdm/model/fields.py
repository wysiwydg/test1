"""The field registry: one declaration per canonical attribute.

This module is the single source of truth for the canonical data model. The
Arrow schemas used by the vectorized layer, the Postgres DDL used by the golden
store, and the API contracts are all projected from these declarations. Nothing
downstream re-declares a column, so the three can never drift apart; a test
asserts the committed DDL still matches what the registry emits.

Each :class:`FieldSpec` carries more than a type. It also declares:

*   ``survivorship`` - how the golden value is chosen when sources disagree.
    Expressing this as data rather than as procedural code lets the whole
    survivorship policy be applied as one vectorized group-by aggregation over
    contributing records, and lets it be audited without reading code.
*   ``match_role`` - the part the attribute plays in entity resolution. The
    resolver reads the registry to decide what to block on, what to score, and
    what vetoes a match, so adding a comparator is a one-line change here.
*   ``pii`` - sensitivity, which drives masking, export filtering and retention.
*   ``derived`` - whether the value is computed by this system. Derived fields
    are never accepted from a source payload and are recomputed on every write.

The registry is pure standard library on purpose. It is imported by ingestion
workers, by the API server and by schema-generation scripts, and none of them
should be forced to pull in Arrow or an ORM just to learn what a column means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from cmdm.model.enums import (
    LogicalType as LT,
)
from cmdm.model.enums import (
    MatchRole as MR,
)
from cmdm.model.enums import (
    PiiClass as PII,
)
from cmdm.model.enums import (
    SurvivorshipStrategy as SS,
)

__all__ = [
    "FieldSpec",
    "EntitySpec",
    "POLICY",
    "PERSON",
    "RELATIONSHIP",
    "ENTITIES",
    "entity",
]


# Money scale. Chosen at 4 rather than 2 because unit-linked funds, per-mille
# loadings and FX-converted premiums carry sub-cent precision that rounding at
# ingest would destroy irreversibly.
MONEY_PRECISION = 18
MONEY_SCALE = 4


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Declaration of one canonical attribute."""

    name: str
    dtype: LT
    doc: str
    nullable: bool = True
    survivorship: SS = SS.MOST_TRUSTED_SOURCE
    match_role: MR = MR.NONE
    pii: PII = PII.NONE
    #: Computed by this system. Rejected if supplied by a source payload.
    derived: bool = False
    #: Backed by an enum; projects to an Arrow dictionary and a Postgres enum.
    enum_name: str | None = None
    #: Emit a database index. Set for keys and for blocking columns, which are
    #: probed once per candidate-generation pass over the whole population.
    indexed: bool = False
    #: Participates in the record hash used for change detection. System and
    #: audit columns are excluded so that a re-ingest of unchanged data does not
    #: manufacture a new SCD-2 version.
    in_record_hash: bool = True

    def __post_init__(self) -> None:
        if self.derived and self.survivorship not in (SS.DERIVED, SS.SYSTEM):
            raise ValueError(
                f"{self.name}: derived fields must use DERIVED or SYSTEM survivorship, "
                f"got {self.survivorship}"
            )
        if self.enum_name is not None and self.dtype is not LT.STRING:
            raise ValueError(f"{self.name}: enum-backed fields must be STRING")
        if self.match_role is MR.VETO and self.dtype is LT.RATIO:
            # A veto is evaluated as disagreement between two populated values.
            # A continuous score has no meaningful notion of disagreeing, so a
            # veto declared on one would silently never fire.
            raise ValueError(f"{self.name}: VETO requires a discrete comparable type")


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """A canonical entity: a named, ordered set of fields with a primary key."""

    name: str
    table: str
    primary_key: str
    doc: str
    fields: Sequence[FieldSpec]
    #: Natural key used for deterministic identity within a source system.
    natural_key: Sequence[str] = ()

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"{self.name}: duplicate field names {sorted(dupes)}")
        if self.primary_key not in names:
            raise ValueError(f"{self.name}: primary key {self.primary_key!r} not declared")
        for key in self.natural_key:
            if key not in names:
                raise ValueError(f"{self.name}: natural key part {key!r} not declared")

    @property
    def by_name(self) -> Mapping[str, FieldSpec]:
        return MappingProxyType({f.name: f for f in self.fields})

    def select(
        self,
        *,
        match_role: MR | None = None,
        pii: PII | None = None,
        derived: bool | None = None,
        in_record_hash: bool | None = None,
    ) -> tuple[FieldSpec, ...]:
        """Filter fields by declared metadata.

        This is how the rest of the system asks the model questions: the blocking
        pass asks for ``match_role=BLOCKING``, the export layer asks for
        ``pii=DIRECT`` to mask, and the hasher asks for ``in_record_hash=True``.
        """

        def keep(f: FieldSpec) -> bool:
            return (
                (match_role is None or f.match_role is match_role)
                and (pii is None or f.pii is pii)
                and (derived is None or f.derived is derived)
                and (in_record_hash is None or f.in_record_hash is in_record_hash)
            )

        return tuple(f for f in self.fields if keep(f))


# ---------------------------------------------------------------------------
# Shared column groups
# ---------------------------------------------------------------------------


def _lineage_fields(entity_noun: str) -> list[FieldSpec]:
    """SCD-2 versioning and audit columns carried by every golden entity.

    Golden records are never updated in place. A change closes the current
    version by stamping ``valid_to`` and inserts a new row with an incremented
    ``version``, which is what makes "why did this record look like this in
    March?" answerable and what makes the store safe to replicate.
    """
    return [
        FieldSpec(
            "version",
            LT.INT32,
            f"Monotonic version of this {entity_noun}. Starts at 1 for the first "
            "golden write and increments on every materially changed re-write.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "valid_from",
            LT.TIMESTAMP_TZ,
            "Instant this version became the current one.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            indexed=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "valid_to",
            LT.TIMESTAMP_TZ,
            "Instant this version was superseded. Null on the current version. "
            "Half-open interval: valid_from is inclusive, valid_to exclusive.",
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "is_current",
            LT.BOOL,
            "True on exactly one version per entity id. Redundant with a null "
            "valid_to, kept because it supports a partial unique index that makes "
            "the one-current-version rule a database constraint rather than a "
            "convention the writers are trusted to honour.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            indexed=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "record_hash",
            LT.STRING,
            "BLAKE2b digest over the hashable fields of this version. Change "
            "detection compares hashes, so an unchanged re-ingest is a no-op "
            "instead of a new version.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "source_count",
            LT.INT32,
            "Number of distinct source records that contributed to this version. "
            "A drop in this number between versions is a strong signal that a "
            "feed failed rather than that the world changed.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "confidence",
            LT.RATIO,
            "Aggregate confidence that this golden record represents exactly one "
            "real-world entity. Below the review threshold the record is served "
            "with a warning rather than withheld.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "is_curated",
            LT.BOOL,
            "A steward has manually adjusted this record. Curated attributes are "
            "protected from being overwritten by automated survivorship until the "
            "override is explicitly released.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "is_deleted",
            LT.BOOL,
            "Soft-delete marker. Physical deletes are reserved for erasure "
            "requests, which are executed against the source and golden stores "
            "together and logged as their own audit event.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "created_at",
            LT.TIMESTAMP_TZ,
            "Instant version 1 of this entity was first written.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "updated_at",
            LT.TIMESTAMP_TZ,
            "Instant this version row was written.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            in_record_hash=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

POLICY = EntitySpec(
    name="Policy",
    table="policy",
    primary_key="policy_id",
    natural_key=("source_system", "policy_number_normalized"),
    doc=(
        "The insurance contract, and the unit the source data arrives in. A "
        "policy is the grain of the inbound feed: one row carries the contract "
        "terms plus the party details for each role. Ingestion splits that row "
        "into one Policy, N Person and N Relationship records.\n\n"
        "Policy identity is close to deterministic. A policy number is unique "
        "within its issuing system, so the natural key is "
        "(source_system, policy_number_normalized) and resolution needs no "
        "probabilistic matching. Cross-source policy consolidation, which does "
        "arise after a book transfer or a carrier migration, is handled by an "
        "explicit crosswalk rather than by scoring."
    ),
    fields=[
        FieldSpec(
            "policy_id",
            LT.UUID,
            "Surrogate golden key. UUIDv7, so it sorts by creation time and keeps "
            "index writes and Arrow row groups local instead of scattering them.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            indexed=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "policy_number",
            LT.STRING,
            "Policy number exactly as the source presents it, punctuation and all.",
            nullable=False,
            survivorship=SS.MOST_TRUSTED_SOURCE,
            match_role=MR.IDENTIFIER,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "policy_number_normalized",
            LT.STRING,
            "Upper-cased, with separators and leading zeros in the numeric tail "
            "stripped. The join key. Kept beside the raw value because 'POL-001234' "
            "and 'pol1234' are the same contract and only one of the two forms can "
            "be displayed back to a user unchanged.",
            nullable=False,
            survivorship=SS.DERIVED,
            match_role=MR.IDENTIFIER,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "source_system",
            LT.STRING,
            "Identifier of the originating system. Scopes the natural key and "
            "selects the trust weight used by survivorship.",
            nullable=False,
            survivorship=SS.MOST_TRUSTED_SOURCE,
            indexed=True,
        ),
        FieldSpec(
            "product_code",
            LT.STRING,
            "Carrier product code as sourced.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "product_name",
            LT.STRING,
            "Marketing name of the product.",
            survivorship=SS.MOST_COMPLETE,
        ),
        FieldSpec(
            "product_line",
            LT.STRING,
            "Broad product family, normalized across carriers.",
            survivorship=SS.MOST_FREQUENT,
            enum_name="ProductLine",
            indexed=True,
        ),
        FieldSpec(
            "plan_code",
            LT.STRING,
            "Plan or rider bundle within the product.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "policy_status",
            LT.STRING,
            "Lifecycle state of the contract.",
            survivorship=SS.MOST_RECENT,
            enum_name="PolicyStatus",
            indexed=True,
        ),
        FieldSpec(
            "status_reason",
            LT.STRING,
            "Carrier reason code for the current status.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "application_date",
            LT.DATE,
            "Date the application was signed.",
            survivorship=SS.AGGREGATE_MIN,
        ),
        FieldSpec(
            "issue_date",
            LT.DATE,
            "Date the carrier issued the contract.",
            survivorship=SS.AGGREGATE_MIN,
        ),
        FieldSpec(
            "effective_date",
            LT.DATE,
            "Date cover began. Bounds the default validity window of the "
            "relationships attached to this policy.",
            survivorship=SS.AGGREGATE_MIN,
            indexed=True,
        ),
        FieldSpec(
            "maturity_date",
            LT.DATE,
            "Scheduled maturity or expiry of cover.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "termination_date",
            LT.DATE,
            "Date cover actually ended, however it ended. Null while in force.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "paid_to_date",
            LT.DATE,
            "Date premiums are paid up to.",
            survivorship=SS.AGGREGATE_MAX,
        ),
        FieldSpec(
            "last_anniversary_date",
            LT.DATE,
            "Most recent policy anniversary.",
            survivorship=SS.AGGREGATE_MAX,
        ),
        FieldSpec(
            "currency_code",
            LT.STRING,
            "ISO 4217 code for every monetary amount on this record. Amounts are "
            "stored as issued and never converted, because a converted premium "
            "cannot be reconciled against the carrier's ledger.",
            survivorship=SS.MOST_FREQUENT,
        ),
        FieldSpec(
            "sum_assured_amount",
            LT.MONEY,
            "Contractual benefit payable on the insured event.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "annual_premium_amount",
            LT.MONEY,
            "Annualized premium, normalized from the billing mode.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "modal_premium_amount",
            LT.MONEY,
            "Premium billed per instalment at the stated frequency.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "account_value_amount",
            LT.MONEY,
            "Current account or fund value for investment-linked contracts.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "surrender_value_amount",
            LT.MONEY,
            "Current cash surrender value.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "premium_frequency",
            LT.STRING,
            "Billing mode: how often a premium instalment falls due.",
            survivorship=SS.MOST_RECENT,
            enum_name="PremiumFrequency",
        ),
        FieldSpec(
            "payment_method",
            LT.STRING,
            "How premiums are collected, such as direct debit or payroll.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "policy_term_years",
            LT.INT16,
            "Contract term in years. Null for whole-of-life.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "premium_paying_term_years",
            LT.INT16,
            "Years over which premiums are payable.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "issuing_company_code",
            LT.STRING,
            "Legal entity that issued the contract. Distinct from source_system: "
            "one administration platform commonly issues for several carriers.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
        ),
        FieldSpec(
            "branch_code",
            LT.STRING,
            "Servicing branch or office.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "distribution_channel",
            LT.STRING,
            "Channel the policy was sold through.",
            survivorship=SS.MOST_FREQUENT,
        ),
        FieldSpec(
            "underwriting_class",
            LT.STRING,
            "Risk class assigned at underwriting.",
            survivorship=SS.MOST_RECENT,
            pii=PII.SENSITIVE,
        ),
        FieldSpec(
            "issue_jurisdiction",
            LT.STRING,
            "State or country whose regulations govern the contract.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
        ),
        FieldSpec(
            "party_count",
            LT.INT16,
            "Number of current party relationships attached to this policy. "
            "Maintained by the relationship writer so that a policy that has lost "
            "its owner is detectable without a join.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "data_quality_score",
            LT.RATIO,
            "Share of business-critical attributes populated on this record.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        *_lineage_fields("policy"),
    ],
)


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------

PERSON = EntitySpec(
    name="Person",
    table="person",
    primary_key="person_id",
    doc=(
        "The party: a natural person or a legal entity acting in a policy role.\n\n"
        "Person has no natural key of its own. The source identifiers, "
        "OwnerCustomerId, InsuredCustomerId and AgentCode, are authoritative "
        "within their source system but not across systems, and the same human "
        "may hold all three in different capacities. Person identity therefore "
        "lives in the person_xref crosswalk: every (source_system, id_kind, "
        "source_key) triple points at a person_id, many to one. Deterministic "
        "matching consumes the crosswalk; probabilistic matching extends it.\n\n"
        "Names arrive as a single string with no component breakdown, so every "
        "matchable name key on this entity is derived. The normalized forms, "
        "token set, phonetic key and parsed components are all computed by this "
        "system, stored beside the raw name rather than replacing it, and "
        "recomputed whenever the normalization rules change."
    ),
    fields=[
        FieldSpec(
            "person_id",
            LT.UUID,
            "Surrogate golden key. UUIDv7. Survives merges: when two persons are "
            "merged the loser's id is retired into person_xref pointing at the "
            "winner, so previously issued ids never dangle.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            indexed=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "party_type",
            LT.STRING,
            "Whether this party is a natural person or a legal entity. Gates the "
            "comparators the resolver applies: date of birth and given/surname "
            "similarity are not evaluated for organizations.",
            nullable=False,
            survivorship=SS.MOST_FREQUENT,
            match_role=MR.VETO,
            enum_name="PartyType",
            indexed=True,
        ),
        FieldSpec(
            "full_name",
            LT.STRING,
            "Name exactly as sourced, as one string. The system of record for the "
            "name; every other name column on this entity is derived from it.",
            nullable=False,
            survivorship=SS.MOST_COMPLETE,
            match_role=MR.COMPARATOR,
            pii=PII.DIRECT,
        ),
        FieldSpec(
            "full_name_normalized",
            LT.STRING,
            "Case-folded, accent-stripped, punctuation-collapsed, with honorifics "
            "and generational suffixes removed. The form comparators run against.",
            nullable=False,
            survivorship=SS.DERIVED,
            match_role=MR.COMPARATOR,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "name_tokens",
            LT.LIST_STRING,
            "Normalized name split into tokens. Held as a list rather than a "
            "string so that token-set similarity is an array operation instead of "
            "a re-split on every comparison.",
            nullable=False,
            survivorship=SS.DERIVED,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "name_sorted_key",
            LT.STRING,
            "Name tokens sorted and rejoined. Makes 'John Michael Smith' and "
            "'Smith John Michael' collide, which is the common failure mode when "
            "feeds disagree about name order.",
            nullable=False,
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.DIRECT,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "name_phonetic_key",
            LT.STRING,
            "Double-metaphone codes of the name tokens, sorted and joined. Blocks "
            "together spellings that sound alike, which is what recovers the "
            "transcription errors that exact keys miss.",
            nullable=False,
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.DIRECT,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "name_initials",
            LT.STRING,
            "First letter of each name token, in order. A cheap, high-recall "
            "blocking key for records whose name is heavily abbreviated.",
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.INDIRECT,
            derived=True,
        ),
        FieldSpec(
            "given_name_derived",
            LT.STRING,
            "Inferred given name. Never authoritative: the source has no component "
            "breakdown, so this is a guess carrying its own confidence.",
            survivorship=SS.DERIVED,
            match_role=MR.COMPARATOR,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "middle_name_derived",
            LT.STRING,
            "Inferred middle name or names.",
            survivorship=SS.DERIVED,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "surname_derived",
            LT.STRING,
            "Inferred surname. Carries the same caveat as the given name: the "
            "source never supplied it as a distinct field.",
            survivorship=SS.DERIVED,
            match_role=MR.COMPARATOR,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "name_prefix_derived",
            LT.STRING,
            "Honorific stripped during normalization, retained for display.",
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "name_suffix_derived",
            LT.STRING,
            "Generational or professional suffix stripped during normalization. "
            "Worth keeping: a Jr/Sr difference between two otherwise identical "
            "names is evidence of two people, not one.",
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "name_parse_confidence",
            LT.RATIO,
            "Confidence in the component split. Comparators weight the derived "
            "components by this, so a doubtful parse cannot drive a merge.",
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "name_parse_method",
            LT.STRING,
            "Which engine produced the split. Lets a parser upgrade be re-run over "
            "only the records the cheap path handled badly.",
            nullable=False,
            survivorship=SS.DERIVED,
            enum_name="NameParseMethod",
            derived=True,
        ),
        FieldSpec(
            "date_of_birth",
            LT.DATE,
            "Date of birth. The strongest natural-person comparator available, and "
            "a veto: two records with different populated dates of birth are not "
            "the same person regardless of how well their names agree.",
            survivorship=SS.MOST_FREQUENT,
            match_role=MR.VETO,
            pii=PII.DIRECT,
            indexed=True,
        ),
        FieldSpec(
            "date_of_death",
            LT.DATE,
            "Date of death where known. Drives suppression of outbound contact.",
            survivorship=SS.MOST_RECENT,
            pii=PII.SENSITIVE,
        ),
        FieldSpec(
            "gender",
            LT.STRING,
            "Gender as recorded by the carrier. A weak comparator, since it "
            "agrees by chance half the time.",
            survivorship=SS.MOST_FREQUENT,
            match_role=MR.COMPARATOR,
            enum_name="Gender",
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "marital_status",
            LT.STRING,
            "Marital status as recorded.",
            survivorship=SS.MOST_RECENT,
            enum_name="MaritalStatus",
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "national_id_type",
            LT.STRING,
            "Kind of government identifier the hash below was computed over. "
            "Required to compare two hashes meaningfully.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
            enum_name="NationalIdType",
        ),
        FieldSpec(
            "national_id_hash",
            LT.STRING,
            "Keyed BLAKE2b digest of the normalized identifier. The identifier "
            "itself is never stored in the golden record: the hash supports exact "
            "matching and blocking, which is all resolution needs, without holding "
            "the regulated value where a query can reach it.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
            match_role=MR.IDENTIFIER,
            pii=PII.SENSITIVE,
            indexed=True,
        ),
        FieldSpec(
            "national_id_last4",
            LT.STRING,
            "Last four characters, for steward review screens where a bare hash "
            "would make a merge decision impossible to sanity-check.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
            pii=PII.SENSITIVE,
        ),
        FieldSpec(
            "email_address",
            LT.STRING,
            "Email address exactly as the source presents it, before normalization.",
            survivorship=SS.MOST_RECENT,
            pii=PII.DIRECT,
        ),
        FieldSpec(
            "email_normalized",
            LT.STRING,
            "Lower-cased, with provider-specific aliasing folded away.",
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.DIRECT,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "phone_raw",
            LT.STRING,
            "Phone number as sourced.",
            survivorship=SS.MOST_RECENT,
            pii=PII.DIRECT,
        ),
        FieldSpec(
            "phone_e164",
            LT.STRING,
            "Phone in E.164, defaulting to the policy jurisdiction when the source "
            "omits a country code.",
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.DIRECT,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "address_line1",
            LT.STRING,
            "First line of the postal address.",
            survivorship=SS.MOST_RECENT,
            pii=PII.DIRECT,
        ),
        FieldSpec(
            "address_line2",
            LT.STRING,
            "Second line of the postal address.",
            survivorship=SS.MOST_RECENT,
            pii=PII.DIRECT,
        ),
        FieldSpec(
            "city",
            LT.STRING,
            "City, town or locality of the postal address.",
            survivorship=SS.MOST_RECENT,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "state_province",
            LT.STRING,
            "State, province or region.",
            survivorship=SS.MOST_RECENT,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "postal_code",
            LT.STRING,
            "Postal or ZIP code. A useful comparator on its own, since two "
            "similar names in the same small postcode are far more likely to be "
            "one person.",
            survivorship=SS.MOST_RECENT,
            match_role=MR.COMPARATOR,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "country_code",
            LT.STRING,
            "ISO 3166-1 alpha-2 country code.",
            survivorship=SS.MOST_RECENT,
            pii=PII.NONE,
        ),
        FieldSpec(
            "address_normalized",
            LT.STRING,
            "Address flattened to a single normalized string with thoroughfare "
            "types and unit designators standardized.",
            survivorship=SS.DERIVED,
            match_role=MR.COMPARATOR,
            pii=PII.DIRECT,
            derived=True,
        ),
        FieldSpec(
            "address_key",
            LT.STRING,
            "Compact hash of the normalized address plus postal code. Blocks "
            "co-resident parties together, which is the backbone of both "
            "householding and same-address duplicate detection.",
            survivorship=SS.DERIVED,
            match_role=MR.BLOCKING,
            pii=PII.INDIRECT,
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "occupation",
            LT.STRING,
            "Occupation as stated on the application. Weak evidence for "
            "matching, retained for underwriting and segmentation.",
            survivorship=SS.MOST_RECENT,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "nationality",
            LT.STRING,
            "Stated nationality as an ISO 3166-1 alpha-2 code.",
            survivorship=SS.MOST_RECENT,
            pii=PII.INDIRECT,
        ),
        FieldSpec(
            "customer_since_date",
            LT.DATE,
            "Earliest effective date across the policies this party is attached "
            "to. Takes the minimum, because a later feed cannot make someone a "
            "newer customer than they already were.",
            survivorship=SS.AGGREGATE_MIN,
            derived=False,
        ),
        FieldSpec(
            "is_deceased",
            LT.BOOL,
            "Deceased flag. ANY_TRUE: one source asserting a death must not be "
            "outvoted by feeds that simply have not caught up.",
            nullable=False,
            survivorship=SS.ANY_TRUE,
        ),
        FieldSpec(
            "do_not_contact",
            LT.BOOL,
            "Marketing suppression. ANY_TRUE for the same reason, with a "
            "regulatory edge: an opt-out lost in a merge is a compliance breach.",
            nullable=False,
            survivorship=SS.ANY_TRUE,
        ),
        FieldSpec(
            "is_sanctioned",
            LT.BOOL,
            "Screening hit against a sanctions or PEP list. ANY_TRUE.",
            nullable=False,
            survivorship=SS.ANY_TRUE,
            pii=PII.SENSITIVE,
        ),
        FieldSpec(
            "policy_count",
            LT.INT32,
            "Number of current policies this party is attached to in any role.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "role_bitmap",
            LT.INT32,
            "Bitmask of the roles this party has ever held. Answers 'is this "
            "person also an agent?' without touching the relationship table, "
            "which matters because that question drives conflict-of-interest "
            "checks over the whole population at once.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "data_quality_score",
            LT.RATIO,
            "Share of matchable attributes populated. Low-scoring records are "
            "held out of automatic merging, because a record with only a common "
            "name and nothing else will match too many things.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        *_lineage_fields("person"),
    ],
)


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------

RELATIONSHIP = EntitySpec(
    name="Relationship",
    table="relationship",
    primary_key="relationship_id",
    doc=(
        "The edge that carries role. One logical entity in two shapes, "
        "discriminated by edge_kind.\n\n"
        "PARTY_POLICY edges are asserted by the source: they record that a "
        "person is the owner, insured or agent of a policy, together with the "
        "source identifier that made the claim. This is the many-to-many that "
        "the requirement describes, with role as a first-class attribute rather "
        "than as three columns on Policy.\n\n"
        "PARTY_PARTY edges are derived by traversing PARTY_POLICY edges: two "
        "insureds on one policy are co-insured, an owner and an insured on one "
        "policy are connected, everyone an agent writes for is serviced by that "
        "agent. They are stored rather than computed on read because the "
        "traversal is expensive and the results drive householding and "
        "cross-sell queries that run constantly. Every derived edge names the "
        "policies that evidence it and is fully recomputable.\n\n"
        "Edges are versioned on the same SCD-2 basis as the entities, so a "
        "change of agent is a closed version and a new one rather than an "
        "overwrite, and the servicing history stays answerable."
    ),
    fields=[
        FieldSpec(
            "relationship_id",
            LT.UUID,
            "Surrogate key for the edge. UUIDv7.",
            nullable=False,
            survivorship=SS.SYSTEM,
            derived=True,
            indexed=True,
            in_record_hash=False,
        ),
        FieldSpec(
            "edge_kind",
            LT.STRING,
            "PARTY_POLICY for sourced role edges, PARTY_PARTY for derived ones. "
            "Exactly one of to_policy_id and to_person_id is populated, enforced "
            "by a check constraint rather than by writer discipline.",
            nullable=False,
            survivorship=SS.SYSTEM,
            enum_name="EdgeKind",
            indexed=True,
        ),
        FieldSpec(
            "from_person_id",
            LT.UUID,
            "The party the edge originates from.",
            nullable=False,
            survivorship=SS.SYSTEM,
            indexed=True,
        ),
        FieldSpec(
            "to_policy_id",
            LT.UUID,
            "Target policy for PARTY_POLICY edges. Null otherwise.",
            survivorship=SS.SYSTEM,
            indexed=True,
        ),
        FieldSpec(
            "to_person_id",
            LT.UUID,
            "Target party for PARTY_PARTY edges. Null otherwise.",
            survivorship=SS.SYSTEM,
            indexed=True,
        ),
        FieldSpec(
            "role",
            LT.STRING,
            "Capacity the party acts in on the policy. Populated for "
            "PARTY_POLICY edges.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
            enum_name="PartyRole",
            indexed=True,
        ),
        FieldSpec(
            "association_type",
            LT.STRING,
            "Nature of the inferred link. Populated for PARTY_PARTY edges.",
            survivorship=SS.DERIVED,
            enum_name="AssociationType",
            derived=True,
            indexed=True,
        ),
        FieldSpec(
            "role_sequence",
            LT.INT16,
            "Ordinal within a repeated role, distinguishing first from second "
            "insured. Preserves source ordering, which carries meaning the role "
            "alone does not.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
        ),
        FieldSpec(
            "source_party_key",
            LT.STRING,
            "The identifier the source used for this party in this role: "
            "OwnerCustomerId, InsuredCustomerId or AgentCode. Retained on the "
            "edge, not just in the crosswalk, so the exact assertion the source "
            "made stays reconstructable after a merge moves the person_id.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
            match_role=MR.IDENTIFIER,
            pii=PII.INDIRECT,
            indexed=True,
        ),
        FieldSpec(
            "source_key_kind",
            LT.STRING,
            "Which identifier namespace source_party_key belongs to. The same "
            "literal value can be a valid OwnerCustomerId and a valid AgentCode "
            "for different parties, so the namespace is part of the key.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
        ),
        FieldSpec(
            "source_system",
            LT.STRING,
            "System that asserted this edge.",
            nullable=False,
            survivorship=SS.MOST_TRUSTED_SOURCE,
            indexed=True,
        ),
        FieldSpec(
            "ownership_percent",
            LT.RATIO,
            "Share of ownership for joint owners.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "benefit_percent",
            LT.RATIO,
            "Share of benefit for beneficiary edges.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "effective_from",
            LT.DATE,
            "Real-world date the party took this role. Distinct from valid_from, "
            "which is when this system learned it. Keeping the two apart is what "
            "makes a backdated agent-of-record change representable.",
            survivorship=SS.MOST_TRUSTED_SOURCE,
        ),
        FieldSpec(
            "effective_to",
            LT.DATE,
            "Real-world date the party ceased this role. Null while current.",
            survivorship=SS.MOST_RECENT,
        ),
        FieldSpec(
            "evidence_policy_ids",
            LT.LIST_UUID,
            "Policies that evidence a derived PARTY_PARTY edge. Empty for sourced "
            "edges. Makes every inference traceable to the facts behind it.",
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "evidence_count",
            LT.INT32,
            "Number of distinct policies supporting a derived edge. Two people "
            "sharing five policies is a much stronger signal than sharing one.",
            nullable=False,
            survivorship=SS.DERIVED,
            derived=True,
        ),
        FieldSpec(
            "derivation_method",
            LT.STRING,
            "How the edge was established, from an exact key match through to the "
            "local-model fallback. The audit trail for anything AI touched.",
            nullable=False,
            survivorship=SS.SYSTEM,
            enum_name="DerivationMethod",
            indexed=True,
        ),
        *_lineage_fields("relationship"),
    ],
)


ENTITIES: Mapping[str, EntitySpec] = MappingProxyType(
    {e.name: e for e in (POLICY, PERSON, RELATIONSHIP)}
)


def entity(name: str) -> EntitySpec:
    """Look up an entity spec by name, with a useful error when it is missing."""
    try:
        return ENTITIES[name]
    except KeyError:
        raise KeyError(f"unknown entity {name!r}; known: {sorted(ENTITIES)}") from None


def all_fields() -> Iterable[tuple[EntitySpec, FieldSpec]]:
    """Iterate every (entity, field) pair in the model."""
    for spec in ENTITIES.values():
        for f in spec.fields:
            yield spec, f
