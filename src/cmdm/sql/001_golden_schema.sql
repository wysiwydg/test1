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

CREATE SCHEMA IF NOT EXISTS mdm;

SET search_path TO mdm, public;


-- Controlled vocabularies. Declared as Postgres enums so an unmapped
-- source value is rejected at write time rather than accumulating as
-- free text nobody notices until a report is wrong.

CREATE TYPE mdm.party_type AS ENUM (
    'PERSON',
    'ORGANIZATION',
    'TRUST',
    'ESTATE',
    'UNKNOWN'
);

CREATE TYPE mdm.party_role AS ENUM (
    'OWNER',
    'JOINT_OWNER',
    'INSURED',
    'JOINT_INSURED',
    'AGENT',
    'WRITING_AGENT',
    'SERVICING_AGENT',
    'BENEFICIARY',
    'CONTINGENT_BENEFICIARY',
    'PAYER',
    'PAYOR_BANK',
    'ASSIGNEE',
    'UNKNOWN'
);

CREATE TYPE mdm.association_type AS ENUM (
    'CO_INSURED',
    'CO_OWNER',
    'OWNER_OF_INSURED',
    'INSURED_OF_OWNER',
    'SERVICED_BY_AGENT',
    'AGENT_SERVICES',
    'HOUSEHOLD_MEMBER',
    'SUSPECTED_DUPLICATE'
);

CREATE TYPE mdm.edge_kind AS ENUM (
    'PARTY_POLICY',
    'PARTY_PARTY'
);

CREATE TYPE mdm.policy_status AS ENUM (
    'PROPOSED',
    'UNDERWRITING',
    'ISSUED',
    'INFORCE',
    'PAID_UP',
    'GRACE',
    'LAPSED',
    'REINSTATED',
    'SURRENDERED',
    'MATURED',
    'CLAIM_PENDING',
    'DEATH_CLAIM',
    'EXPIRED',
    'CANCELLED',
    'NOT_TAKEN_UP',
    'UNKNOWN'
);

CREATE TYPE mdm.product_line AS ENUM (
    'LIFE',
    'ANNUITY',
    'HEALTH',
    'DISABILITY',
    'CRITICAL_ILLNESS',
    'AUTO',
    'PROPERTY',
    'LIABILITY',
    'TRAVEL',
    'GROUP',
    'OTHER',
    'UNKNOWN'
);

CREATE TYPE mdm.premium_frequency AS ENUM (
    'SINGLE',
    'ANNUAL',
    'SEMI_ANNUAL',
    'QUARTERLY',
    'MONTHLY',
    'FORTNIGHTLY',
    'WEEKLY',
    'UNKNOWN'
);

CREATE TYPE mdm.gender AS ENUM (
    'MALE',
    'FEMALE',
    'OTHER',
    'UNKNOWN'
);

CREATE TYPE mdm.marital_status AS ENUM (
    'SINGLE',
    'MARRIED',
    'DIVORCED',
    'WIDOWED',
    'SEPARATED',
    'DOMESTIC_PARTNER',
    'UNKNOWN'
);

CREATE TYPE mdm.national_id_type AS ENUM (
    'SSN',
    'TIN',
    'NRIC',
    'PASSPORT',
    'DRIVING_LICENCE',
    'NATIONAL_ID',
    'COMPANY_REG_NO',
    'OTHER',
    'UNKNOWN'
);

CREATE TYPE mdm.name_parse_method AS ENUM (
    'NOT_PARSED',
    'RULE_BASED',
    'STATISTICAL',
    'LLM_FALLBACK',
    'MANUAL'
);

CREATE TYPE mdm.derivation_method AS ENUM (
    'DETERMINISTIC',
    'PROBABILISTIC',
    'EMBEDDING',
    'AI_FALLBACK',
    'MANUAL',
    'INHERITED'
);

CREATE TYPE mdm.match_decision AS ENUM (
    'MATCH',
    'NO_MATCH',
    'REVIEW',
    'BLOCKED_BY_RULE'
);

CREATE TYPE mdm.survivorship_strategy AS ENUM (
    'MOST_RECENT',
    'MOST_TRUSTED_SOURCE',
    'MOST_COMPLETE',
    'MOST_FREQUENT',
    'FIRST_NON_NULL',
    'AGGREGATE_MAX',
    'AGGREGATE_MIN',
    'ANY_TRUE',
    'DERIVED',
    'SYSTEM'
);

-- PersonMaster. Identity anchor for person. Every person_id ever issued, including
-- those retired by a merge.
CREATE TABLE mdm.person_master (
    person_id       uuid NOT NULL,
    created_at      timestamptz NOT NULL,
    is_active       boolean NOT NULL,
    merged_into_id  uuid,
    CONSTRAINT pk_person_master PRIMARY KEY (person_id)
);

COMMENT ON COLUMN mdm.person_master.person_id IS 'The surrogate identity itself. One row per entity, created once and never reused.';
COMMENT ON COLUMN mdm.person_master.created_at IS 'When the identity was minted.';
COMMENT ON COLUMN mdm.person_master.is_active IS 'False once the identity has been merged away. The row stays so that ids already published to consumers keep resolving.';
COMMENT ON COLUMN mdm.person_master.merged_into_id IS 'Winning identity, when this one lost a merge. Null otherwise. Following this pointer is how a retired id resolves to the surviving record.';

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_person_master_is_active
    ON mdm.person_master (is_active);
CREATE INDEX ix_person_master_merged_into_id
    ON mdm.person_master (merged_into_id);

-- PolicyMaster. Identity anchor for policy. Every policy_id ever issued, including
-- those retired by a cross-source consolidation.
CREATE TABLE mdm.policy_master (
    policy_id       uuid NOT NULL,
    created_at      timestamptz NOT NULL,
    is_active       boolean NOT NULL,
    merged_into_id  uuid,
    CONSTRAINT pk_policy_master PRIMARY KEY (policy_id)
);

COMMENT ON COLUMN mdm.policy_master.policy_id IS 'The surrogate identity itself. One row per entity, created once and never reused.';
COMMENT ON COLUMN mdm.policy_master.created_at IS 'When the identity was minted.';
COMMENT ON COLUMN mdm.policy_master.is_active IS 'False once the identity has been merged away. The row stays so that ids already published to consumers keep resolving.';
COMMENT ON COLUMN mdm.policy_master.merged_into_id IS 'Winning identity, when this one lost a merge. Null otherwise. Following this pointer is how a retired id resolves to the surviving record.';

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_policy_master_is_active
    ON mdm.policy_master (is_active);
CREATE INDEX ix_policy_master_merged_into_id
    ON mdm.policy_master (merged_into_id);

-- Policy. The insurance contract, and the unit the source data arrives in. A policy is
-- the grain of the inbound feed: one row carries the contract terms plus the party
-- details for each role. Ingestion splits that row into one Policy, N Person and N
-- Relationship records.
--
-- Policy identity is close to deterministic. A policy number is unique within its
-- issuing system, so the natural key is (source_system, policy_number_normalized) and
-- resolution needs no probabilistic matching. Cross-source policy consolidation, which
-- does arise after a book transfer or a carrier migration, is handled by an explicit
-- crosswalk rather than by scoring.
CREATE TABLE mdm.policy (
    row_id            bigint GENERATED ALWAYS AS IDENTITY,
    policy_id                  uuid NOT NULL,
    policy_number              text NOT NULL,
    policy_number_normalized   text NOT NULL,
    source_system              text NOT NULL,
    product_code               text,
    product_name               text,
    product_line               product_line,
    plan_code                  text,
    policy_status              policy_status,
    status_reason              text,
    application_date           date,
    issue_date                 date,
    effective_date             date,
    maturity_date              date,
    termination_date           date,
    paid_to_date               date,
    last_anniversary_date      date,
    currency_code              text,
    sum_assured_amount         numeric(18, 4),
    annual_premium_amount      numeric(18, 4),
    modal_premium_amount       numeric(18, 4),
    account_value_amount       numeric(18, 4),
    surrender_value_amount     numeric(18, 4),
    premium_frequency          premium_frequency,
    payment_method             text,
    policy_term_years          smallint,
    premium_paying_term_years  smallint,
    issuing_company_code       text,
    branch_code                text,
    distribution_channel       text,
    underwriting_class         text,
    issue_jurisdiction         text,
    party_count                smallint NOT NULL,
    data_quality_score         double precision NOT NULL,
    version                    integer NOT NULL,
    valid_from                 timestamptz NOT NULL,
    valid_to                   timestamptz,
    is_current                 boolean NOT NULL,
    record_hash                text NOT NULL,
    source_count               integer NOT NULL,
    confidence                 double precision NOT NULL,
    is_curated                 boolean NOT NULL,
    is_deleted                 boolean NOT NULL,
    created_at                 timestamptz NOT NULL,
    updated_at                 timestamptz NOT NULL,
    CONSTRAINT pk_policy PRIMARY KEY (policy_id, version),
    CONSTRAINT ck_policy_validity CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_policy_current CHECK (is_current = (valid_to IS NULL))
);

COMMENT ON COLUMN mdm.policy.policy_id IS 'Surrogate golden key. UUIDv7, so it sorts by creation time and keeps index writes and Arrow row groups local instead of scattering them.';
COMMENT ON COLUMN mdm.policy.policy_number IS 'Policy number exactly as the source presents it, punctuation and all.';
COMMENT ON COLUMN mdm.policy.policy_number_normalized IS 'Upper-cased, with separators and leading zeros in the numeric tail stripped. The join key. Kept beside the raw value because ''POL-001234'' and ''pol1234'' are the same contract and only one of the two forms can be displayed back to a user unchanged.';
COMMENT ON COLUMN mdm.policy.source_system IS 'Identifier of the originating system. Scopes the natural key and selects the trust weight used by survivorship.';
COMMENT ON COLUMN mdm.policy.product_code IS 'Carrier product code as sourced.';
COMMENT ON COLUMN mdm.policy.product_name IS 'Marketing name of the product.';
COMMENT ON COLUMN mdm.policy.product_line IS 'Broad product family, normalized across carriers.';
COMMENT ON COLUMN mdm.policy.plan_code IS 'Plan or rider bundle within the product.';
COMMENT ON COLUMN mdm.policy.policy_status IS 'Lifecycle state of the contract.';
COMMENT ON COLUMN mdm.policy.status_reason IS 'Carrier reason code for the current status.';
COMMENT ON COLUMN mdm.policy.application_date IS 'Date the application was signed.';
COMMENT ON COLUMN mdm.policy.issue_date IS 'Date the carrier issued the contract.';
COMMENT ON COLUMN mdm.policy.effective_date IS 'Date cover began. Bounds the default validity window of the relationships attached to this policy.';
COMMENT ON COLUMN mdm.policy.maturity_date IS 'Scheduled maturity or expiry of cover.';
COMMENT ON COLUMN mdm.policy.termination_date IS 'Date cover actually ended, however it ended. Null while in force.';
COMMENT ON COLUMN mdm.policy.paid_to_date IS 'Date premiums are paid up to.';
COMMENT ON COLUMN mdm.policy.last_anniversary_date IS 'Most recent policy anniversary.';
COMMENT ON COLUMN mdm.policy.currency_code IS 'ISO 4217 code for every monetary amount on this record. Amounts are stored as issued and never converted, because a converted premium cannot be reconciled against the carrier''s ledger.';
COMMENT ON COLUMN mdm.policy.sum_assured_amount IS 'Contractual benefit payable on the insured event.';
COMMENT ON COLUMN mdm.policy.annual_premium_amount IS 'Annualized premium, normalized from the billing mode.';
COMMENT ON COLUMN mdm.policy.modal_premium_amount IS 'Premium billed per instalment at the stated frequency.';
COMMENT ON COLUMN mdm.policy.account_value_amount IS 'Current account or fund value for investment-linked contracts.';
COMMENT ON COLUMN mdm.policy.surrender_value_amount IS 'Current cash surrender value.';
COMMENT ON COLUMN mdm.policy.premium_frequency IS 'Billing mode: how often a premium instalment falls due.';
COMMENT ON COLUMN mdm.policy.payment_method IS 'How premiums are collected, such as direct debit or payroll.';
COMMENT ON COLUMN mdm.policy.policy_term_years IS 'Contract term in years. Null for whole-of-life.';
COMMENT ON COLUMN mdm.policy.premium_paying_term_years IS 'Years over which premiums are payable.';
COMMENT ON COLUMN mdm.policy.issuing_company_code IS 'Legal entity that issued the contract. Distinct from source_system: one administration platform commonly issues for several carriers.';
COMMENT ON COLUMN mdm.policy.branch_code IS 'Servicing branch or office.';
COMMENT ON COLUMN mdm.policy.distribution_channel IS 'Channel the policy was sold through.';
COMMENT ON COLUMN mdm.policy.underwriting_class IS 'Risk class assigned at underwriting.';
COMMENT ON COLUMN mdm.policy.issue_jurisdiction IS 'State or country whose regulations govern the contract.';
COMMENT ON COLUMN mdm.policy.party_count IS 'Number of current party relationships attached to this policy. Maintained by the relationship writer so that a policy that has lost its owner is detectable without a join.';
COMMENT ON COLUMN mdm.policy.data_quality_score IS 'Share of business-critical attributes populated on this record.';
COMMENT ON COLUMN mdm.policy.version IS 'Monotonic version of this policy. Starts at 1 for the first golden write and increments on every materially changed re-write.';
COMMENT ON COLUMN mdm.policy.valid_from IS 'Instant this version became the current one.';
COMMENT ON COLUMN mdm.policy.valid_to IS 'Instant this version was superseded. Null on the current version. Half-open interval: valid_from is inclusive, valid_to exclusive.';
COMMENT ON COLUMN mdm.policy.is_current IS 'True on exactly one version per entity id. Redundant with a null valid_to, kept because it supports a partial unique index that makes the one-current-version rule a database constraint rather than a convention the writers are trusted to honour.';
COMMENT ON COLUMN mdm.policy.record_hash IS 'BLAKE2b digest over the hashable fields of this version. Change detection compares hashes, so an unchanged re-ingest is a no-op instead of a new version.';
COMMENT ON COLUMN mdm.policy.source_count IS 'Number of distinct source records that contributed to this version. A drop in this number between versions is a strong signal that a feed failed rather than that the world changed.';
COMMENT ON COLUMN mdm.policy.confidence IS 'Aggregate confidence that this golden record represents exactly one real-world entity. Below the review threshold the record is served with a warning rather than withheld.';
COMMENT ON COLUMN mdm.policy.is_curated IS 'A steward has manually adjusted this record. Curated attributes are protected from being overwritten by automated survivorship until the override is explicitly released.';
COMMENT ON COLUMN mdm.policy.is_deleted IS 'Soft-delete marker. Physical deletes are reserved for erasure requests, which are executed against the source and golden stores together and logged as their own audit event.';
COMMENT ON COLUMN mdm.policy.created_at IS 'Instant version 1 of this entity was first written.';
COMMENT ON COLUMN mdm.policy.updated_at IS 'Instant this version row was written.';

-- Exactly one current version per entity, enforced by the database
-- rather than by writer discipline.
CREATE UNIQUE INDEX uq_policy_current
    ON mdm.policy (policy_id)
    WHERE is_current;

-- Natural key: deterministic identity within a source system.
CREATE UNIQUE INDEX uq_policy_natural
    ON mdm.policy (source_system, policy_number_normalized)
    WHERE is_current;

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_policy_policy_number_normalized
    ON mdm.policy (policy_number_normalized)
    WHERE is_current;
CREATE INDEX ix_policy_source_system
    ON mdm.policy (source_system)
    WHERE is_current;
CREATE INDEX ix_policy_product_line
    ON mdm.policy (product_line)
    WHERE is_current;
CREATE INDEX ix_policy_policy_status
    ON mdm.policy (policy_status)
    WHERE is_current;
CREATE INDEX ix_policy_effective_date
    ON mdm.policy (effective_date)
    WHERE is_current;
CREATE INDEX ix_policy_valid_from
    ON mdm.policy (valid_from)
    WHERE is_current;
CREATE INDEX ix_policy_is_current
    ON mdm.policy (is_current)
    WHERE is_current;

-- Person. The party: a natural person or a legal entity acting in a policy role.
--
-- Person has no natural key of its own. The source identifiers, OwnerCustomerId,
-- InsuredCustomerId and AgentCode, are authoritative within their source system but not
-- across systems, and the same human may hold all three in different capacities. Person
-- identity therefore lives in the person_xref crosswalk: every (source_system, id_kind,
-- source_key) triple points at a person_id, many to one. Deterministic matching
-- consumes the crosswalk; probabilistic matching extends it.
--
-- Names arrive as a single string with no component breakdown, so every matchable name
-- key on this entity is derived. The normalized forms, token set, phonetic key and
-- parsed components are all computed by this system, stored beside the raw name rather
-- than replacing it, and recomputed whenever the normalization rules change.
CREATE TABLE mdm.person (
    row_id            bigint GENERATED ALWAYS AS IDENTITY,
    person_id              uuid NOT NULL,
    party_type             party_type NOT NULL,
    full_name              text NOT NULL,
    full_name_normalized   text NOT NULL,
    name_tokens            text[] NOT NULL,
    name_sorted_key        text NOT NULL,
    name_phonetic_key      text NOT NULL,
    name_initials          text,
    given_name_derived     text,
    middle_name_derived    text,
    surname_derived        text,
    name_prefix_derived    text,
    name_suffix_derived    text,
    name_parse_confidence  double precision,
    name_parse_method      name_parse_method NOT NULL,
    date_of_birth          date,
    date_of_death          date,
    gender                 gender,
    marital_status         marital_status,
    national_id_type       national_id_type,
    national_id_hash       text,
    national_id_last4      text,
    email_address          text,
    email_normalized       text,
    phone_raw              text,
    phone_e164             text,
    address_line1          text,
    address_line2          text,
    city                   text,
    state_province         text,
    postal_code            text,
    country_code           text,
    address_normalized     text,
    address_key            text,
    occupation             text,
    nationality            text,
    customer_since_date    date,
    is_deceased            boolean NOT NULL,
    do_not_contact         boolean NOT NULL,
    is_sanctioned          boolean NOT NULL,
    policy_count           integer NOT NULL,
    role_bitmap            integer NOT NULL,
    data_quality_score     double precision NOT NULL,
    version                integer NOT NULL,
    valid_from             timestamptz NOT NULL,
    valid_to               timestamptz,
    is_current             boolean NOT NULL,
    record_hash            text NOT NULL,
    source_count           integer NOT NULL,
    confidence             double precision NOT NULL,
    is_curated             boolean NOT NULL,
    is_deleted             boolean NOT NULL,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    CONSTRAINT pk_person PRIMARY KEY (person_id, version),
    CONSTRAINT ck_person_validity CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_person_current CHECK (is_current = (valid_to IS NULL))
);

COMMENT ON COLUMN mdm.person.person_id IS 'Surrogate golden key. UUIDv7. Survives merges: when two persons are merged the loser''s id is retired into person_xref pointing at the winner, so previously issued ids never dangle.';
COMMENT ON COLUMN mdm.person.party_type IS 'Whether this party is a natural person or a legal entity. Gates the comparators the resolver applies: date of birth and given/surname similarity are not evaluated for organizations.';
COMMENT ON COLUMN mdm.person.full_name IS 'Name exactly as sourced, as one string. The system of record for the name; every other name column on this entity is derived from it.';
COMMENT ON COLUMN mdm.person.full_name_normalized IS 'Case-folded, accent-stripped, punctuation-collapsed, with honorifics and generational suffixes removed. The form comparators run against.';
COMMENT ON COLUMN mdm.person.name_tokens IS 'Normalized name split into tokens. Held as a list rather than a string so that token-set similarity is an array operation instead of a re-split on every comparison.';
COMMENT ON COLUMN mdm.person.name_sorted_key IS 'Name tokens sorted and rejoined. Makes ''John Michael Smith'' and ''Smith John Michael'' collide, which is the common failure mode when feeds disagree about name order.';
COMMENT ON COLUMN mdm.person.name_phonetic_key IS 'Double-metaphone codes of the name tokens, sorted and joined. Blocks together spellings that sound alike, which is what recovers the transcription errors that exact keys miss.';
COMMENT ON COLUMN mdm.person.name_initials IS 'First letter of each name token, in order. A cheap, high-recall blocking key for records whose name is heavily abbreviated.';
COMMENT ON COLUMN mdm.person.given_name_derived IS 'Inferred given name. Never authoritative: the source has no component breakdown, so this is a guess carrying its own confidence.';
COMMENT ON COLUMN mdm.person.middle_name_derived IS 'Inferred middle name or names.';
COMMENT ON COLUMN mdm.person.surname_derived IS 'Inferred surname. Carries the same caveat as the given name: the source never supplied it as a distinct field.';
COMMENT ON COLUMN mdm.person.name_prefix_derived IS 'Honorific stripped during normalization, retained for display.';
COMMENT ON COLUMN mdm.person.name_suffix_derived IS 'Generational or professional suffix stripped during normalization. Worth keeping: a Jr/Sr difference between two otherwise identical names is evidence of two people, not one.';
COMMENT ON COLUMN mdm.person.name_parse_confidence IS 'Confidence in the component split. Comparators weight the derived components by this, so a doubtful parse cannot drive a merge.';
COMMENT ON COLUMN mdm.person.name_parse_method IS 'Which engine produced the split. Lets a parser upgrade be re-run over only the records the cheap path handled badly.';
COMMENT ON COLUMN mdm.person.date_of_birth IS 'Date of birth. The strongest natural-person comparator available, and a veto: two records with different populated dates of birth are not the same person regardless of how well their names agree.';
COMMENT ON COLUMN mdm.person.date_of_death IS 'Date of death where known. Drives suppression of outbound contact.';
COMMENT ON COLUMN mdm.person.gender IS 'Gender as recorded by the carrier. A weak comparator, since it agrees by chance half the time.';
COMMENT ON COLUMN mdm.person.marital_status IS 'Marital status as recorded.';
COMMENT ON COLUMN mdm.person.national_id_type IS 'Kind of government identifier the hash below was computed over. Required to compare two hashes meaningfully.';
COMMENT ON COLUMN mdm.person.national_id_hash IS 'Keyed BLAKE2b digest of the normalized identifier. The identifier itself is never stored in the golden record: the hash supports exact matching and blocking, which is all resolution needs, without holding the regulated value where a query can reach it.';
COMMENT ON COLUMN mdm.person.national_id_last4 IS 'Last four characters, for steward review screens where a bare hash would make a merge decision impossible to sanity-check.';
COMMENT ON COLUMN mdm.person.email_address IS 'Email address exactly as the source presents it, before normalization.';
COMMENT ON COLUMN mdm.person.email_normalized IS 'Lower-cased, with provider-specific aliasing folded away.';
COMMENT ON COLUMN mdm.person.phone_raw IS 'Phone number as sourced.';
COMMENT ON COLUMN mdm.person.phone_e164 IS 'Phone in E.164, defaulting to the policy jurisdiction when the source omits a country code.';
COMMENT ON COLUMN mdm.person.address_line1 IS 'First line of the postal address.';
COMMENT ON COLUMN mdm.person.address_line2 IS 'Second line of the postal address.';
COMMENT ON COLUMN mdm.person.city IS 'City, town or locality of the postal address.';
COMMENT ON COLUMN mdm.person.state_province IS 'State, province or region.';
COMMENT ON COLUMN mdm.person.postal_code IS 'Postal or ZIP code. A useful comparator on its own, since two similar names in the same small postcode are far more likely to be one person.';
COMMENT ON COLUMN mdm.person.country_code IS 'ISO 3166-1 alpha-2 country code.';
COMMENT ON COLUMN mdm.person.address_normalized IS 'Address flattened to a single normalized string with thoroughfare types and unit designators standardized.';
COMMENT ON COLUMN mdm.person.address_key IS 'Compact hash of the normalized address plus postal code. Blocks co-resident parties together, which is the backbone of both householding and same-address duplicate detection.';
COMMENT ON COLUMN mdm.person.occupation IS 'Occupation as stated on the application. Weak evidence for matching, retained for underwriting and segmentation.';
COMMENT ON COLUMN mdm.person.nationality IS 'Stated nationality as an ISO 3166-1 alpha-2 code.';
COMMENT ON COLUMN mdm.person.customer_since_date IS 'Earliest effective date across the policies this party is attached to. Takes the minimum, because a later feed cannot make someone a newer customer than they already were.';
COMMENT ON COLUMN mdm.person.is_deceased IS 'Deceased flag. ANY_TRUE: one source asserting a death must not be outvoted by feeds that simply have not caught up.';
COMMENT ON COLUMN mdm.person.do_not_contact IS 'Marketing suppression. ANY_TRUE for the same reason, with a regulatory edge: an opt-out lost in a merge is a compliance breach.';
COMMENT ON COLUMN mdm.person.is_sanctioned IS 'Screening hit against a sanctions or PEP list. ANY_TRUE.';
COMMENT ON COLUMN mdm.person.policy_count IS 'Number of current policies this party is attached to in any role.';
COMMENT ON COLUMN mdm.person.role_bitmap IS 'Bitmask of the roles this party has ever held. Answers ''is this person also an agent?'' without touching the relationship table, which matters because that question drives conflict-of-interest checks over the whole population at once.';
COMMENT ON COLUMN mdm.person.data_quality_score IS 'Share of matchable attributes populated. Low-scoring records are held out of automatic merging, because a record with only a common name and nothing else will match too many things.';
COMMENT ON COLUMN mdm.person.version IS 'Monotonic version of this person. Starts at 1 for the first golden write and increments on every materially changed re-write.';
COMMENT ON COLUMN mdm.person.valid_from IS 'Instant this version became the current one.';
COMMENT ON COLUMN mdm.person.valid_to IS 'Instant this version was superseded. Null on the current version. Half-open interval: valid_from is inclusive, valid_to exclusive.';
COMMENT ON COLUMN mdm.person.is_current IS 'True on exactly one version per entity id. Redundant with a null valid_to, kept because it supports a partial unique index that makes the one-current-version rule a database constraint rather than a convention the writers are trusted to honour.';
COMMENT ON COLUMN mdm.person.record_hash IS 'BLAKE2b digest over the hashable fields of this version. Change detection compares hashes, so an unchanged re-ingest is a no-op instead of a new version.';
COMMENT ON COLUMN mdm.person.source_count IS 'Number of distinct source records that contributed to this version. A drop in this number between versions is a strong signal that a feed failed rather than that the world changed.';
COMMENT ON COLUMN mdm.person.confidence IS 'Aggregate confidence that this golden record represents exactly one real-world entity. Below the review threshold the record is served with a warning rather than withheld.';
COMMENT ON COLUMN mdm.person.is_curated IS 'A steward has manually adjusted this record. Curated attributes are protected from being overwritten by automated survivorship until the override is explicitly released.';
COMMENT ON COLUMN mdm.person.is_deleted IS 'Soft-delete marker. Physical deletes are reserved for erasure requests, which are executed against the source and golden stores together and logged as their own audit event.';
COMMENT ON COLUMN mdm.person.created_at IS 'Instant version 1 of this entity was first written.';
COMMENT ON COLUMN mdm.person.updated_at IS 'Instant this version row was written.';

-- Exactly one current version per entity, enforced by the database
-- rather than by writer discipline.
CREATE UNIQUE INDEX uq_person_current
    ON mdm.person (person_id)
    WHERE is_current;

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_person_party_type
    ON mdm.person (party_type)
    WHERE is_current;
CREATE INDEX ix_person_name_sorted_key
    ON mdm.person (name_sorted_key)
    WHERE is_current;
CREATE INDEX ix_person_name_phonetic_key
    ON mdm.person (name_phonetic_key)
    WHERE is_current;
CREATE INDEX ix_person_date_of_birth
    ON mdm.person (date_of_birth)
    WHERE is_current;
CREATE INDEX ix_person_national_id_hash
    ON mdm.person (national_id_hash)
    WHERE is_current;
CREATE INDEX ix_person_email_normalized
    ON mdm.person (email_normalized)
    WHERE is_current;
CREATE INDEX ix_person_phone_e164
    ON mdm.person (phone_e164)
    WHERE is_current;
CREATE INDEX ix_person_address_key
    ON mdm.person (address_key)
    WHERE is_current;
CREATE INDEX ix_person_valid_from
    ON mdm.person (valid_from)
    WHERE is_current;
CREATE INDEX ix_person_is_current
    ON mdm.person (is_current)
    WHERE is_current;

-- Relationship. The edge that carries role. One logical entity in two shapes,
-- discriminated by edge_kind.
--
-- PARTY_POLICY edges are asserted by the source: they record that a person is the
-- owner, insured or agent of a policy, together with the source identifier that made
-- the claim. This is the many-to-many that the requirement describes, with role as a
-- first-class attribute rather than as three columns on Policy.
--
-- PARTY_PARTY edges are derived by traversing PARTY_POLICY edges: two insureds on one
-- policy are co-insured, an owner and an insured on one policy are connected, everyone
-- an agent writes for is serviced by that agent. They are stored rather than computed
-- on read because the traversal is expensive and the results drive householding and
-- cross-sell queries that run constantly. Every derived edge names the policies that
-- evidence it and is fully recomputable.
--
-- Edges are versioned on the same SCD-2 basis as the entities, so a change of agent is
-- a closed version and a new one rather than an overwrite, and the servicing history
-- stays answerable.
CREATE TABLE mdm.relationship (
    row_id            bigint GENERATED ALWAYS AS IDENTITY,
    relationship_id      uuid NOT NULL,
    edge_kind            edge_kind NOT NULL,
    from_person_id       uuid NOT NULL,
    to_policy_id         uuid,
    to_person_id         uuid,
    role                 party_role,
    association_type     association_type,
    role_sequence        smallint,
    source_party_key     text,
    source_key_kind      text,
    source_system        text NOT NULL,
    ownership_percent    double precision,
    benefit_percent      double precision,
    effective_from       date,
    effective_to         date,
    evidence_policy_ids  uuid[],
    evidence_count       integer NOT NULL,
    derivation_method    derivation_method NOT NULL,
    version              integer NOT NULL,
    valid_from           timestamptz NOT NULL,
    valid_to             timestamptz,
    is_current           boolean NOT NULL,
    record_hash          text NOT NULL,
    source_count         integer NOT NULL,
    confidence           double precision NOT NULL,
    is_curated           boolean NOT NULL,
    is_deleted           boolean NOT NULL,
    created_at           timestamptz NOT NULL,
    updated_at           timestamptz NOT NULL,
    CONSTRAINT pk_relationship PRIMARY KEY (relationship_id, version),
    CONSTRAINT ck_relationship_target CHECK (
        (edge_kind = 'PARTY_POLICY' AND to_policy_id IS NOT NULL AND to_person_id IS NULL)
        OR (edge_kind = 'PARTY_PARTY' AND to_person_id IS NOT NULL AND to_policy_id IS NULL)
    ),
    CONSTRAINT ck_relationship_role CHECK (
        (edge_kind = 'PARTY_POLICY' AND role IS NOT NULL)
        OR (edge_kind = 'PARTY_PARTY' AND association_type IS NOT NULL)
    ),
    CONSTRAINT ck_relationship_no_self CHECK (
        to_person_id IS DISTINCT FROM from_person_id
    ),
    CONSTRAINT ck_relationship_validity CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_relationship_current CHECK (is_current = (valid_to IS NULL))
);

COMMENT ON COLUMN mdm.relationship.relationship_id IS 'Surrogate key for the edge. UUIDv7.';
COMMENT ON COLUMN mdm.relationship.edge_kind IS 'PARTY_POLICY for sourced role edges, PARTY_PARTY for derived ones. Exactly one of to_policy_id and to_person_id is populated, enforced by a check constraint rather than by writer discipline.';
COMMENT ON COLUMN mdm.relationship.from_person_id IS 'The party the edge originates from.';
COMMENT ON COLUMN mdm.relationship.to_policy_id IS 'Target policy for PARTY_POLICY edges. Null otherwise.';
COMMENT ON COLUMN mdm.relationship.to_person_id IS 'Target party for PARTY_PARTY edges. Null otherwise.';
COMMENT ON COLUMN mdm.relationship.role IS 'Capacity the party acts in on the policy. Populated for PARTY_POLICY edges.';
COMMENT ON COLUMN mdm.relationship.association_type IS 'Nature of the inferred link. Populated for PARTY_PARTY edges.';
COMMENT ON COLUMN mdm.relationship.role_sequence IS 'Ordinal within a repeated role, distinguishing first from second insured. Preserves source ordering, which carries meaning the role alone does not.';
COMMENT ON COLUMN mdm.relationship.source_party_key IS 'The identifier the source used for this party in this role: OwnerCustomerId, InsuredCustomerId or AgentCode. Retained on the edge, not just in the crosswalk, so the exact assertion the source made stays reconstructable after a merge moves the person_id.';
COMMENT ON COLUMN mdm.relationship.source_key_kind IS 'Which identifier namespace source_party_key belongs to. The same literal value can be a valid OwnerCustomerId and a valid AgentCode for different parties, so the namespace is part of the key.';
COMMENT ON COLUMN mdm.relationship.source_system IS 'System that asserted this edge.';
COMMENT ON COLUMN mdm.relationship.ownership_percent IS 'Share of ownership for joint owners.';
COMMENT ON COLUMN mdm.relationship.benefit_percent IS 'Share of benefit for beneficiary edges.';
COMMENT ON COLUMN mdm.relationship.effective_from IS 'Real-world date the party took this role. Distinct from valid_from, which is when this system learned it. Keeping the two apart is what makes a backdated agent-of-record change representable.';
COMMENT ON COLUMN mdm.relationship.effective_to IS 'Real-world date the party ceased this role. Null while current.';
COMMENT ON COLUMN mdm.relationship.evidence_policy_ids IS 'Policies that evidence a derived PARTY_PARTY edge. Empty for sourced edges. Makes every inference traceable to the facts behind it.';
COMMENT ON COLUMN mdm.relationship.evidence_count IS 'Number of distinct policies supporting a derived edge. Two people sharing five policies is a much stronger signal than sharing one.';
COMMENT ON COLUMN mdm.relationship.derivation_method IS 'How the edge was established, from an exact key match through to the local-model fallback. The audit trail for anything AI touched.';
COMMENT ON COLUMN mdm.relationship.version IS 'Monotonic version of this relationship. Starts at 1 for the first golden write and increments on every materially changed re-write.';
COMMENT ON COLUMN mdm.relationship.valid_from IS 'Instant this version became the current one.';
COMMENT ON COLUMN mdm.relationship.valid_to IS 'Instant this version was superseded. Null on the current version. Half-open interval: valid_from is inclusive, valid_to exclusive.';
COMMENT ON COLUMN mdm.relationship.is_current IS 'True on exactly one version per entity id. Redundant with a null valid_to, kept because it supports a partial unique index that makes the one-current-version rule a database constraint rather than a convention the writers are trusted to honour.';
COMMENT ON COLUMN mdm.relationship.record_hash IS 'BLAKE2b digest over the hashable fields of this version. Change detection compares hashes, so an unchanged re-ingest is a no-op instead of a new version.';
COMMENT ON COLUMN mdm.relationship.source_count IS 'Number of distinct source records that contributed to this version. A drop in this number between versions is a strong signal that a feed failed rather than that the world changed.';
COMMENT ON COLUMN mdm.relationship.confidence IS 'Aggregate confidence that this golden record represents exactly one real-world entity. Below the review threshold the record is served with a warning rather than withheld.';
COMMENT ON COLUMN mdm.relationship.is_curated IS 'A steward has manually adjusted this record. Curated attributes are protected from being overwritten by automated survivorship until the override is explicitly released.';
COMMENT ON COLUMN mdm.relationship.is_deleted IS 'Soft-delete marker. Physical deletes are reserved for erasure requests, which are executed against the source and golden stores together and logged as their own audit event.';
COMMENT ON COLUMN mdm.relationship.created_at IS 'Instant version 1 of this entity was first written.';
COMMENT ON COLUMN mdm.relationship.updated_at IS 'Instant this version row was written.';

-- Exactly one current version per entity, enforced by the database
-- rather than by writer discipline.
CREATE UNIQUE INDEX uq_relationship_current
    ON mdm.relationship (relationship_id)
    WHERE is_current;

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_relationship_edge_kind
    ON mdm.relationship (edge_kind)
    WHERE is_current;
CREATE INDEX ix_relationship_from_person_id
    ON mdm.relationship (from_person_id)
    WHERE is_current;
CREATE INDEX ix_relationship_to_policy_id
    ON mdm.relationship (to_policy_id)
    WHERE is_current;
CREATE INDEX ix_relationship_to_person_id
    ON mdm.relationship (to_person_id)
    WHERE is_current;
CREATE INDEX ix_relationship_role
    ON mdm.relationship (role)
    WHERE is_current;
CREATE INDEX ix_relationship_association_type
    ON mdm.relationship (association_type)
    WHERE is_current;
CREATE INDEX ix_relationship_source_party_key
    ON mdm.relationship (source_party_key)
    WHERE is_current;
CREATE INDEX ix_relationship_source_system
    ON mdm.relationship (source_system)
    WHERE is_current;
CREATE INDEX ix_relationship_derivation_method
    ON mdm.relationship (derivation_method)
    WHERE is_current;
CREATE INDEX ix_relationship_valid_from
    ON mdm.relationship (valid_from)
    WHERE is_current;
CREATE INDEX ix_relationship_is_current
    ON mdm.relationship (is_current)
    WHERE is_current;

-- SourceRecord. Immutable landing zone. One row per inbound policy record, exactly as
-- received.
--
-- Nothing in this table is ever updated or interpreted. It exists so that the entire
-- golden layer can be dropped and rebuilt deterministically, and so that any disputed
-- golden value can be traced to the literal bytes that produced it. Content addressing
-- by payload hash makes re-delivery of an unchanged file a no-op rather than a
-- duplicate.
CREATE TABLE mdm.source_record (
    source_record_id  uuid NOT NULL,
    source_system     text NOT NULL,
    source_batch_id   text NOT NULL,
    source_row_key    text NOT NULL,
    payload_hash      text NOT NULL,
    payload           jsonb NOT NULL,
    source_timestamp  timestamptz NOT NULL,
    ingested_at       timestamptz NOT NULL,
    is_processed      boolean NOT NULL,
    reject_reason     text,
    CONSTRAINT pk_source_record PRIMARY KEY (source_record_id)
);

COMMENT ON COLUMN mdm.source_record.source_record_id IS 'Surrogate key for the landed row.';
COMMENT ON COLUMN mdm.source_record.source_system IS 'System the record came from.';
COMMENT ON COLUMN mdm.source_record.source_batch_id IS 'Ingestion batch or file identifier.';
COMMENT ON COLUMN mdm.source_record.source_row_key IS 'Natural key of the row within its batch, normally the policy number.';
COMMENT ON COLUMN mdm.source_record.payload_hash IS 'BLAKE2b digest of the canonicalized payload. Re-delivery of an identical row is recognized and skipped on this column.';
COMMENT ON COLUMN mdm.source_record.payload IS 'The record as received, with original field names and values. Stored as JSONB so that a source schema change lands without a migration and stays queryable.';
COMMENT ON COLUMN mdm.source_record.source_timestamp IS 'When the source asserts this record was last changed. Drives MOST_RECENT survivorship; falls back to ingest time when the source omits it, which is itself recorded as a quality flag.';
COMMENT ON COLUMN mdm.source_record.ingested_at IS 'When this system received the record.';
COMMENT ON COLUMN mdm.source_record.is_processed IS 'Whether the record has been projected into the golden layer.';
COMMENT ON COLUMN mdm.source_record.reject_reason IS 'Why the record could not be projected. Rejects stay in place rather than being discarded, so a bad feed is diagnosable.';

-- Natural key: deterministic identity within a source system.
CREATE UNIQUE INDEX uq_source_record_natural
    ON mdm.source_record (source_system, source_batch_id, source_row_key);

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_source_record_source_system
    ON mdm.source_record (source_system);
CREATE INDEX ix_source_record_source_batch_id
    ON mdm.source_record (source_batch_id);
CREATE INDEX ix_source_record_source_row_key
    ON mdm.source_record (source_row_key);
CREATE INDEX ix_source_record_payload_hash
    ON mdm.source_record (payload_hash);
CREATE INDEX ix_source_record_source_timestamp
    ON mdm.source_record (source_timestamp);
CREATE INDEX ix_source_record_is_processed
    ON mdm.source_record (is_processed);

-- PersonXref. The identity crosswalk, and the answer to how the three source party
-- identifiers become one golden person.
--
-- Every (source_system, source_key_kind, source_party_key) triple resolves to exactly
-- one person_id; many triples resolve to the same person. That is what lets an
-- OwnerCustomerId from one platform and an InsuredCustomerId from another collapse into
-- a single party once matching establishes they are the same human, without either
-- source identifier being lost or rewritten.
--
-- Retired person_ids also live here. When two persons merge, the loser's id is inserted
-- as a RETIRED_ID row pointing at the winner, so ids handed out to downstream consumers
-- keep resolving after the merge.
CREATE TABLE mdm.person_xref (
    xref_id            uuid NOT NULL,
    person_id          uuid NOT NULL,
    source_system      text NOT NULL,
    source_key_kind    text NOT NULL,
    source_party_key   text NOT NULL,
    is_active          boolean NOT NULL,
    linked_at          timestamptz NOT NULL,
    linked_by          text NOT NULL,
    derivation_method  derivation_method NOT NULL,
    confidence         double precision NOT NULL,
    CONSTRAINT pk_person_xref PRIMARY KEY (xref_id)
);

COMMENT ON COLUMN mdm.person_xref.xref_id IS 'Surrogate key for this crosswalk entry.';
COMMENT ON COLUMN mdm.person_xref.person_id IS 'Golden person this key currently resolves to.';
COMMENT ON COLUMN mdm.person_xref.source_system IS 'System that issued the key.';
COMMENT ON COLUMN mdm.person_xref.source_key_kind IS 'Identifier namespace: OWNER_CUSTOMER_ID, INSURED_CUSTOMER_ID, AGENT_CODE or RETIRED_ID. Part of the key because the same literal can be valid in more than one namespace.';
COMMENT ON COLUMN mdm.person_xref.source_party_key IS 'The identifier value itself.';
COMMENT ON COLUMN mdm.person_xref.is_active IS 'False once the mapping has been superseded by a merge or split. Rows are never deleted, so the crosswalk''s own history is intact.';
COMMENT ON COLUMN mdm.person_xref.linked_at IS 'When this mapping was established.';
COMMENT ON COLUMN mdm.person_xref.linked_by IS 'Process or steward that established it.';
COMMENT ON COLUMN mdm.person_xref.derivation_method IS 'How the link was decided, from exact key match to AI fallback.';
COMMENT ON COLUMN mdm.person_xref.confidence IS 'Confidence in this specific link.';

-- Natural key: deterministic identity within a source system.
CREATE UNIQUE INDEX uq_person_xref_natural
    ON mdm.person_xref (source_system, source_key_kind, source_party_key);

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_person_xref_person_id
    ON mdm.person_xref (person_id);
CREATE INDEX ix_person_xref_source_system
    ON mdm.person_xref (source_system);
CREATE INDEX ix_person_xref_source_key_kind
    ON mdm.person_xref (source_key_kind);
CREATE INDEX ix_person_xref_source_party_key
    ON mdm.person_xref (source_party_key);
CREATE INDEX ix_person_xref_is_active
    ON mdm.person_xref (is_active);
CREATE INDEX ix_person_xref_derivation_method
    ON mdm.person_xref (derivation_method);

-- PolicyXref. Policy-side crosswalk. Policy numbers are unique within a source, so this
-- is near-trivial in steady state, but it earns its place after a book transfer or
-- platform migration, when the same contract acquires a second policy number under a
-- new administrator and both must keep resolving.
CREATE TABLE mdm.policy_xref (
    xref_id            uuid NOT NULL,
    policy_id          uuid NOT NULL,
    source_system      text NOT NULL,
    source_policy_key  text NOT NULL,
    is_active          boolean NOT NULL,
    linked_at          timestamptz NOT NULL,
    derivation_method  derivation_method NOT NULL,
    CONSTRAINT pk_policy_xref PRIMARY KEY (xref_id)
);

COMMENT ON COLUMN mdm.policy_xref.xref_id IS 'Surrogate key for this crosswalk entry.';
COMMENT ON COLUMN mdm.policy_xref.policy_id IS 'Golden policy this key resolves to.';
COMMENT ON COLUMN mdm.policy_xref.source_system IS 'System that issued the policy number.';
COMMENT ON COLUMN mdm.policy_xref.source_policy_key IS 'Normalized source policy number.';
COMMENT ON COLUMN mdm.policy_xref.is_active IS 'False once superseded.';
COMMENT ON COLUMN mdm.policy_xref.linked_at IS 'When the mapping was established.';
COMMENT ON COLUMN mdm.policy_xref.derivation_method IS 'How the link was decided.';

-- Natural key: deterministic identity within a source system.
CREATE UNIQUE INDEX uq_policy_xref_natural
    ON mdm.policy_xref (source_system, source_policy_key);

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_policy_xref_policy_id
    ON mdm.policy_xref (policy_id);
CREATE INDEX ix_policy_xref_source_system
    ON mdm.policy_xref (source_system);
CREATE INDEX ix_policy_xref_source_policy_key
    ON mdm.policy_xref (source_policy_key);

-- AttributeProvenance. Per-attribute survivorship record: for one field on one version
-- of one golden record, which source contributed the winning value and under which
-- rule.
--
-- This is the table that answers the only question stewards actually ask, which is not
-- 'what is the golden value' but 'why is it that and not the other one'. It is narrow
-- and tall by design so that it stays cheap to write in bulk from a vectorized
-- survivorship pass, and it stores the losing candidates alongside the winner so a
-- disputed field can be re-adjudicated without re-reading the source layer.
CREATE TABLE mdm.attribute_provenance (
    provenance_id             uuid NOT NULL,
    entity_name               text NOT NULL,
    entity_id                 uuid NOT NULL,
    entity_version            integer NOT NULL,
    attribute_name            text NOT NULL,
    winning_source_record_id  uuid NOT NULL,
    winning_source_system     text NOT NULL,
    strategy                  survivorship_strategy NOT NULL,
    value_text                text,
    candidate_count           integer NOT NULL,
    rejected_values           jsonb,
    decided_at                timestamptz NOT NULL,
    CONSTRAINT pk_attribute_provenance PRIMARY KEY (provenance_id)
);

COMMENT ON COLUMN mdm.attribute_provenance.provenance_id IS 'Surrogate key for this survivorship decision.';
COMMENT ON COLUMN mdm.attribute_provenance.entity_name IS 'Policy, Person or Relationship.';
COMMENT ON COLUMN mdm.attribute_provenance.entity_id IS 'Golden id of the record.';
COMMENT ON COLUMN mdm.attribute_provenance.entity_version IS 'Version of the record this applies to.';
COMMENT ON COLUMN mdm.attribute_provenance.attribute_name IS 'Canonical field name.';
COMMENT ON COLUMN mdm.attribute_provenance.winning_source_record_id IS 'Source record the surviving value came from.';
COMMENT ON COLUMN mdm.attribute_provenance.winning_source_system IS 'Source system the winning record came from, denormalized so that provenance reads need no join back to the landing zone.';
COMMENT ON COLUMN mdm.attribute_provenance.strategy IS 'Survivorship rule that selected it.';
COMMENT ON COLUMN mdm.attribute_provenance.value_text IS 'The winning value rendered as text. Denormalized deliberately: one column beats one per type, and this table is read by humans and diff tools, not by the query planner.';
COMMENT ON COLUMN mdm.attribute_provenance.candidate_count IS 'How many distinct non-null candidates were considered.';
COMMENT ON COLUMN mdm.attribute_provenance.rejected_values IS 'The losing candidates with their sources, so a contested field can be re-adjudicated without going back to the landing zone.';
COMMENT ON COLUMN mdm.attribute_provenance.decided_at IS 'When survivorship ran.';

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_attribute_provenance_entity_name
    ON mdm.attribute_provenance (entity_name);
CREATE INDEX ix_attribute_provenance_entity_id
    ON mdm.attribute_provenance (entity_id);
CREATE INDEX ix_attribute_provenance_attribute_name
    ON mdm.attribute_provenance (attribute_name);

-- MatchAudit. Append-only log of every entity-resolution decision, including
-- non-matches.
--
-- Logging the negatives matters as much as the positives: a duplicate that reaches
-- production is investigated by asking why the pair was compared and rejected, which is
-- unanswerable if only merges are recorded.
--
-- This is also where the AI fallback is held accountable. Rows with derivation_method
-- AI_FALLBACK carry the model identifier, the prompt hash and the raw model output, so
-- every decision a local model made is reproducible, reviewable in isolation, and
-- revocable in bulk if the model turns out to be wrong.
CREATE TABLE mdm.match_audit (
    audit_id             uuid NOT NULL,
    entity_name          text NOT NULL,
    left_id              uuid NOT NULL,
    right_id             uuid NOT NULL,
    decision             match_decision NOT NULL,
    score                double precision,
    blocking_key         text,
    comparator_scores    jsonb,
    derivation_method    derivation_method NOT NULL,
    model_name           text,
    model_version        text,
    prompt_hash          text,
    model_output         jsonb,
    resulting_person_id  uuid,
    reviewed_by          text,
    review_outcome       match_decision,
    decided_at           timestamptz NOT NULL,
    CONSTRAINT pk_match_audit PRIMARY KEY (audit_id)
);

COMMENT ON COLUMN mdm.match_audit.audit_id IS 'Surrogate key for this resolution decision.';
COMMENT ON COLUMN mdm.match_audit.entity_name IS 'Entity the decision concerns.';
COMMENT ON COLUMN mdm.match_audit.left_id IS 'One side of the compared pair.';
COMMENT ON COLUMN mdm.match_audit.right_id IS 'The other side of the compared pair.';
COMMENT ON COLUMN mdm.match_audit.decision IS 'Match, no-match, review or rule-blocked.';
COMMENT ON COLUMN mdm.match_audit.score IS 'Composite similarity score for the pair.';
COMMENT ON COLUMN mdm.match_audit.blocking_key IS 'Blocking key that brought the pair together. Tuning recall means knowing which keys produce which pairs.';
COMMENT ON COLUMN mdm.match_audit.comparator_scores IS 'Per-comparator contributions. Makes a composite score explainable rather than merely reportable.';
COMMENT ON COLUMN mdm.match_audit.derivation_method IS 'Which engine decided.';
COMMENT ON COLUMN mdm.match_audit.model_name IS 'Local model identifier, when the AI fallback was invoked.';
COMMENT ON COLUMN mdm.match_audit.model_version IS 'Model version or digest.';
COMMENT ON COLUMN mdm.match_audit.prompt_hash IS 'Digest of the rendered prompt, so a decision can be replayed against the exact input that produced it.';
COMMENT ON COLUMN mdm.match_audit.model_output IS 'Raw structured output from the model, retained verbatim.';
COMMENT ON COLUMN mdm.match_audit.resulting_person_id IS 'Golden id the pair collapsed into, when the decision was a match.';
COMMENT ON COLUMN mdm.match_audit.reviewed_by IS 'Steward who confirmed or overturned the decision.';
COMMENT ON COLUMN mdm.match_audit.review_outcome IS 'What the steward concluded. Disagreements between this and decision are the labelled training set for tuning thresholds.';
COMMENT ON COLUMN mdm.match_audit.decided_at IS 'When the decision was made.';

-- Blocking keys and lookup columns. These are probed once per
-- candidate-generation pass over the whole population.
CREATE INDEX ix_match_audit_entity_name
    ON mdm.match_audit (entity_name);
CREATE INDEX ix_match_audit_left_id
    ON mdm.match_audit (left_id);
CREATE INDEX ix_match_audit_right_id
    ON mdm.match_audit (right_id);
CREATE INDEX ix_match_audit_decision
    ON mdm.match_audit (decision);
CREATE INDEX ix_match_audit_blocking_key
    ON mdm.match_audit (blocking_key);
CREATE INDEX ix_match_audit_derivation_method
    ON mdm.match_audit (derivation_method);
CREATE INDEX ix_match_audit_resulting_person_id
    ON mdm.match_audit (resulting_person_id);
CREATE INDEX ix_match_audit_decided_at
    ON mdm.match_audit (decided_at);

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
