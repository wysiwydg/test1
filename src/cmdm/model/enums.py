"""Controlled vocabularies for the canonical model.

Every low-cardinality canonical attribute is backed by an enum here rather than a
free-text string. Two reasons that matter downstream:

1.  Vectorization. Enum-backed columns become Arrow dictionary columns, which
    turns group-bys, joins and equality filters into integer work over a small
    dictionary instead of string comparison over millions of rows.
2.  Matching. A comparator that has to reason about "M" vs "Male" vs "1" is a
    comparator that will silently disagree with itself. Normalization into a
    closed vocabulary happens once, at the staging boundary.

Source values that do not map are never dropped. They land in ``UNKNOWN`` and the
raw value is preserved in the source record and in the field's provenance entry,
so an unmapped vocabulary shows up as a data-quality signal instead of silent
loss.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PartyType",
    "PartyRole",
    "AssociationType",
    "EdgeKind",
    "PolicyStatus",
    "ProductLine",
    "PremiumFrequency",
    "Gender",
    "MaritalStatus",
    "NationalIdType",
    "NameParseMethod",
    "DerivationMethod",
    "MatchDecision",
    "SurvivorshipStrategy",
    "PiiClass",
    "MatchRole",
    "LogicalType",
]


class _StrEnum(StrEnum):
    """Base for the model's controlled vocabularies.

    ``StrEnum`` members are genuine ``str`` instances, so they are usable
    directly as Arrow dictionary values and Postgres enum literals with no
    adapter layer, and they render as their value rather than as
    ``ClassName.MEMBER``.
    """


# ---------------------------------------------------------------------------
# Party
# ---------------------------------------------------------------------------


class PartyType(_StrEnum):
    """What kind of legal entity a Person record represents.

    A large share of insurance policy owners are not natural persons: trusts,
    estates, corporations and partnerships routinely own life policies, and
    agents are frequently agencies rather than individuals. Modelling them as a
    separate entity would fragment the relationship graph, so they live in
    Person with a discriminator. The discriminator is load-bearing for matching:
    date-of-birth and given/surname comparators are meaningless for an
    organization and are skipped for those records.
    """

    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    TRUST = "TRUST"
    ESTATE = "ESTATE"
    UNKNOWN = "UNKNOWN"


class PartyRole(_StrEnum):
    """The capacity in which a Person is attached to a Policy.

    The three roles the source data carries are OWNER, INSURED and AGENT. The
    remainder are declared now because they are near-universal in insurance
    extracts and adding an enum member later is cheaper than a schema migration.
    """

    OWNER = "OWNER"
    JOINT_OWNER = "JOINT_OWNER"
    INSURED = "INSURED"
    JOINT_INSURED = "JOINT_INSURED"
    AGENT = "AGENT"
    WRITING_AGENT = "WRITING_AGENT"
    SERVICING_AGENT = "SERVICING_AGENT"
    BENEFICIARY = "BENEFICIARY"
    CONTINGENT_BENEFICIARY = "CONTINGENT_BENEFICIARY"
    PAYER = "PAYER"
    PAYOR_BANK = "PAYOR_BANK"
    ASSIGNEE = "ASSIGNEE"
    UNKNOWN = "UNKNOWN"


#: Roles whose source identifier is authoritative within its source system.
#: These map to OwnerCustomerId, InsuredCustomerId and AgentCode respectively.
IDENTIFIED_ROLES: frozenset[PartyRole] = frozenset(
    {PartyRole.OWNER, PartyRole.INSURED, PartyRole.AGENT}
)


class AssociationType(_StrEnum):
    """Party-to-party edges derived from shared policies.

    These are derived, never stored as delivered. Each carries the policies that
    evidence it so that any derived edge can be traced back to the facts that
    produced it and recomputed from scratch.

    Three groups, and the difference between them decides what a household is.

    *   **Structural** -- CO_INSURED through AGENT_SERVICES. Two parties appear
        on one policy. True by construction and says nothing about how they are
        related: an agent shares a policy with everyone they write for.
    *   **Familial** -- SPOUSE_OF through PARENT_OF. A specific family relation
        the source stated, because insurable interest is a condition of issue
        and every life administration system records it. These are what a
        household is built from.
    *   **Affiliation** -- EMPLOYEE_OF through ESTATE_SUBJECT_OF. A party's link
        to a legal entity. Emphatically *not* a household: the directors a
        company insures do not live together, and treating an employer like a
        family is how a householding pass produces a "household" of forty.
    """

    CO_INSURED = "CO_INSURED"
    CO_OWNER = "CO_OWNER"
    OWNER_OF_INSURED = "OWNER_OF_INSURED"
    INSURED_OF_OWNER = "INSURED_OF_OWNER"
    SERVICED_BY_AGENT = "SERVICED_BY_AGENT"
    AGENT_SERVICES = "AGENT_SERVICES"

    SPOUSE_OF = "SPOUSE_OF"
    CHILD_OF = "CHILD_OF"
    PARENT_OF = "PARENT_OF"
    HOUSEHOLD_MEMBER = "HOUSEHOLD_MEMBER"

    EMPLOYEE_OF = "EMPLOYEE_OF"
    TRUST_MEMBER_OF = "TRUST_MEMBER_OF"
    ESTATE_SUBJECT_OF = "ESTATE_SUBJECT_OF"

    SUSPECTED_DUPLICATE = "SUSPECTED_DUPLICATE"

    @classmethod
    def familial(cls) -> frozenset[AssociationType]:
        """The associations a household may be built from."""
        return frozenset({cls.SPOUSE_OF, cls.CHILD_OF, cls.PARENT_OF})

    @classmethod
    def affiliation(cls) -> frozenset[AssociationType]:
        """A party's link to a legal entity, which is never a household."""
        return frozenset({cls.EMPLOYEE_OF, cls.TRUST_MEMBER_OF,
                          cls.ESTATE_SUBJECT_OF})


class EdgeKind(_StrEnum):
    """Discriminator for the single Relationship entity.

    Relationship is one logical entity with two edge shapes. PARTY_POLICY edges
    are asserted by source data; PARTY_PARTY edges are derived from them. A
    single table with an XOR constraint on the target keeps traversal queries
    uniform and keeps the entity count at the three that were specified.
    """

    PARTY_POLICY = "PARTY_POLICY"
    PARTY_PARTY = "PARTY_PARTY"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class PolicyStatus(_StrEnum):
    """Lifecycle state of a policy contract."""

    PROPOSED = "PROPOSED"
    UNDERWRITING = "UNDERWRITING"
    ISSUED = "ISSUED"
    INFORCE = "INFORCE"
    PAID_UP = "PAID_UP"
    GRACE = "GRACE"
    LAPSED = "LAPSED"
    REINSTATED = "REINSTATED"
    SURRENDERED = "SURRENDERED"
    MATURED = "MATURED"
    CLAIM_PENDING = "CLAIM_PENDING"
    DEATH_CLAIM = "DEATH_CLAIM"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    NOT_TAKEN_UP = "NOT_TAKEN_UP"
    UNKNOWN = "UNKNOWN"


#: Statuses that represent a live contractual obligation. Used by survivorship
#: (an in-force assertion outranks a stale terminated one from a lagging feed)
#: and by relationship validity windows.
ACTIVE_POLICY_STATUSES: frozenset[PolicyStatus] = frozenset(
    {
        PolicyStatus.ISSUED,
        PolicyStatus.INFORCE,
        PolicyStatus.PAID_UP,
        PolicyStatus.GRACE,
        PolicyStatus.REINSTATED,
        PolicyStatus.CLAIM_PENDING,
    }
)


class ProductLine(_StrEnum):
    """Broad product family. Coarser than product_code, stable across carriers."""

    LIFE = "LIFE"
    ANNUITY = "ANNUITY"
    HEALTH = "HEALTH"
    DISABILITY = "DISABILITY"
    CRITICAL_ILLNESS = "CRITICAL_ILLNESS"
    AUTO = "AUTO"
    PROPERTY = "PROPERTY"
    LIABILITY = "LIABILITY"
    TRAVEL = "TRAVEL"
    GROUP = "GROUP"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class PremiumFrequency(_StrEnum):
    """Billing mode. SINGLE means one premium at inception, not annual."""

    SINGLE = "SINGLE"
    ANNUAL = "ANNUAL"
    SEMI_ANNUAL = "SEMI_ANNUAL"
    QUARTERLY = "QUARTERLY"
    MONTHLY = "MONTHLY"
    FORTNIGHTLY = "FORTNIGHTLY"
    WEEKLY = "WEEKLY"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Person demographics
# ---------------------------------------------------------------------------


class Gender(_StrEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class MaritalStatus(_StrEnum):
    SINGLE = "SINGLE"
    MARRIED = "MARRIED"
    DIVORCED = "DIVORCED"
    WIDOWED = "WIDOWED"
    SEPARATED = "SEPARATED"
    DOMESTIC_PARTNER = "DOMESTIC_PARTNER"
    UNKNOWN = "UNKNOWN"


class NationalIdType(_StrEnum):
    """Kind of government identifier, so that the hash column is comparable.

    The identifier value itself is never stored in the golden record; only a
    keyed hash and the last four characters are. Comparing hashes across records
    requires knowing the two hashes are of the same identifier type, which is
    what this column is for.
    """

    SSN = "SSN"
    TIN = "TIN"
    NRIC = "NRIC"
    PASSPORT = "PASSPORT"
    DRIVING_LICENCE = "DRIVING_LICENCE"
    NATIONAL_ID = "NATIONAL_ID"
    COMPANY_REG_NO = "COMPANY_REG_NO"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Resolution provenance
# ---------------------------------------------------------------------------


class NameParseMethod(_StrEnum):
    """How given/surname components were recovered from a full-name string.

    The source supplies names only as a single string, so every component is
    inferred. Recording which engine produced the split lets the match scorer
    discount components that came from a low-confidence path, and lets a later
    parser upgrade be re-run only over the records the cheap path handled badly.
    """

    NOT_PARSED = "NOT_PARSED"
    RULE_BASED = "RULE_BASED"
    STATISTICAL = "STATISTICAL"
    LLM_FALLBACK = "LLM_FALLBACK"
    MANUAL = "MANUAL"


class DerivationMethod(_StrEnum):
    """How a resolution decision or derived edge was produced.

    This is the audit trail for the AI fallback. DETERMINISTIC decisions came
    from an exact key match, PROBABILISTIC from the vectorized scorer, and
    AI_FALLBACK from a local model invoked only on records the scorer left in
    the undecided band. Every AI_FALLBACK row is reviewable in isolation.
    """

    DETERMINISTIC = "DETERMINISTIC"
    PROBABILISTIC = "PROBABILISTIC"
    EMBEDDING = "EMBEDDING"
    AI_FALLBACK = "AI_FALLBACK"
    MANUAL = "MANUAL"
    INHERITED = "INHERITED"


class MatchDecision(_StrEnum):
    """Outcome of comparing a candidate pair.

    REVIEW is a first-class outcome, not a failure. The scorer is deliberately
    allowed to abstain into a band that the AI fallback and then a human queue
    resolve, rather than being forced into a binary call it cannot support.
    """

    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    REVIEW = "REVIEW"
    BLOCKED_BY_RULE = "BLOCKED_BY_RULE"


class SurvivorshipStrategy(_StrEnum):
    """How a single golden value is chosen when sources disagree.

    Declared per attribute in the field registry rather than in procedural code,
    so that the whole survivorship policy is inspectable as data and can be
    applied as a vectorized group-by aggregation over the contributing records.
    """

    #: Value from the contributing record with the newest source timestamp.
    MOST_RECENT = "MOST_RECENT"
    #: Value from the source system with the highest configured trust weight.
    MOST_TRUSTED_SOURCE = "MOST_TRUSTED_SOURCE"
    #: Longest non-null value. Useful for truncated names and addresses.
    MOST_COMPLETE = "MOST_COMPLETE"
    #: Modal value across contributors; ties broken by source trust.
    MOST_FREQUENT = "MOST_FREQUENT"
    #: First non-null in trust order. For values that cannot be re-derived.
    FIRST_NON_NULL = "FIRST_NON_NULL"
    #: Largest value. For monotonically growing amounts and counters.
    AGGREGATE_MAX = "AGGREGATE_MAX"
    #: Smallest value. For earliest-known dates such as customer_since.
    AGGREGATE_MIN = "AGGREGATE_MIN"
    #: Logical OR across contributors. For risk and suppression flags, where a
    #: single source asserting the flag must not be outvoted.
    ANY_TRUE = "ANY_TRUE"
    #: Recomputed from other golden fields, never taken from a source.
    DERIVED = "DERIVED"
    #: Set by the system itself (surrogate keys, versioning, audit columns).
    SYSTEM = "SYSTEM"


class PiiClass(_StrEnum):
    """Sensitivity tier, driving masking, export and retention behaviour."""

    NONE = "NONE"
    #: Identifies a person only in combination with other attributes.
    INDIRECT = "INDIRECT"
    #: Identifies a person on its own.
    DIRECT = "DIRECT"
    #: Special-category or regulated. Never leaves the database in the clear.
    SENSITIVE = "SENSITIVE"


class MatchRole(_StrEnum):
    """The part an attribute plays in entity resolution."""

    #: Exact equality implies identity within a source system.
    IDENTIFIER = "IDENTIFIER"
    #: Cheap key used to restrict the candidate pair space.
    BLOCKING = "BLOCKING"
    #: Contributes a similarity score to the pair.
    COMPARATOR = "COMPARATOR"
    #: Disagreement vetoes a match outright.
    VETO = "VETO"
    #: Carried for business use, ignored by the resolver.
    NONE = "NONE"


class LogicalType(_StrEnum):
    """Storage-neutral type vocabulary.

    The registry declares logical types; the Arrow, Postgres and Python
    projections each map them to their own concrete types. Adding a backend
    means adding one mapping table, not touching field definitions.
    """

    STRING = "STRING"
    BOOL = "BOOL"
    INT16 = "INT16"
    INT32 = "INT32"
    INT64 = "INT64"
    FLOAT32 = "FLOAT32"
    FLOAT64 = "FLOAT64"
    #: Fixed-scale decimal. Money never touches a float in this model.
    MONEY = "MONEY"
    #: 0.0-1.0 confidence score.
    RATIO = "RATIO"
    DATE = "DATE"
    TIMESTAMP_TZ = "TIMESTAMP_TZ"
    UUID = "UUID"
    LIST_STRING = "LIST_STRING"
    LIST_UUID = "LIST_UUID"
    JSON = "JSON"
