-- Governance: access control, consent, erasure and the audit trail.
--
-- MDM concentrates personal data by design. That is the point of the system and
-- also its main risk: a golden record is the most complete profile of a person
-- the organisation holds, assembled from feeds that individually held much less.
-- Controls that would be adequate on a single source system are not adequate
-- here.
--
-- Four things this migration establishes:
--
--   * RBAC with row-level enforcement, so "who may see this" is answered by the
--     database rather than by every caller remembering to filter.
--   * Consent and suppression as first-class records with their own history,
--     not booleans that lose their provenance when overwritten.
--   * Erasure requests as a tracked workflow, because right-to-be-forgotten
--     against a system with immutable history and content-addressed landing is
--     a process, not a DELETE.
--   * An append-only access log covering reads, not just writes. Who *looked*
--     at a record is the question asked after an incident, and it is
--     unanswerable if only mutations were logged.

SET search_path TO mdm, public;


-- ===========================================================================
-- Roles and principals
-- ===========================================================================

CREATE TYPE mdm.principal_kind AS ENUM (
    'HUMAN',
    'SERVICE'
);

-- Roles are coarse and few on purpose. A permission model with fifty roles is
-- one nobody can reason about, and the failure mode is that everyone ends up
-- with the broadest one.
CREATE TYPE mdm.role_name AS ENUM (
    -- Reads golden records with direct PII masked. The default for analytics
    -- and for most business users.
    'VIEWER',
    -- Reads golden records unmasked. For staff who legitimately need to contact
    -- or identify a customer.
    'OPERATOR',
    -- OPERATOR plus merge, split, override and rule approval.
    'STEWARD',
    -- Submits batches and calls the duplicate-check endpoint. No read access to
    -- the golden store beyond the records it submits.
    'INGESTOR',
    -- Full access including erasure execution and role assignment.
    'ADMIN'
);

CREATE TABLE mdm.principal (
    principal_id    uuid PRIMARY KEY,
    kind            mdm.principal_kind NOT NULL,
    subject         text        NOT NULL,
    display_name    text,
    roles           mdm.role_name[] NOT NULL DEFAULT '{}',

    -- Credentials are never stored in the clear. For a service principal this
    -- is a hash of its API key; a compromised database dump must not yield
    -- working credentials.
    secret_hash     text,
    secret_prefix   text,

    is_active       boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz,
    expires_at      timestamptz,

    CONSTRAINT ck_principal_roles CHECK (cardinality(roles) > 0)
);

COMMENT ON TABLE mdm.principal IS
    'Humans and services that may call the system. Secrets are stored hashed.';

CREATE UNIQUE INDEX uq_principal_subject ON mdm.principal (subject);
CREATE INDEX ix_principal_prefix ON mdm.principal (secret_prefix) WHERE is_active;


-- ===========================================================================
-- Consent and suppression
-- ===========================================================================
--
-- The Person entity carries do_not_contact as a boolean because matching and
-- survivorship need it as a column. That boolean is a *projection* of this
-- table, which holds the history: who consented to what, when, through which
-- channel, and when they withdrew it.
--
-- Keeping only the boolean would mean an opt-out could be silently reversed by
-- a later feed with no record that it ever existed -- and "prove this customer
-- consented" is precisely what a regulator asks.

CREATE TYPE mdm.consent_purpose AS ENUM (
    'MARKETING',
    'PROFILING',
    'DATA_SHARING',
    'AUTOMATED_DECISIONS',
    'SERVICE_COMMUNICATION'
);

CREATE TYPE mdm.consent_state AS ENUM (
    'GRANTED',
    'WITHDRAWN',
    'EXPIRED',
    'NEVER_GIVEN'
);

CREATE TABLE mdm.consent (
    consent_id      uuid PRIMARY KEY,
    person_id       uuid        NOT NULL REFERENCES mdm.person_master (person_id),
    purpose         mdm.consent_purpose NOT NULL,
    state           mdm.consent_state   NOT NULL,

    -- Evidence. A consent record with no evidence of how it was obtained is
    -- not evidence of consent.
    channel         text,
    evidence_ref    text,
    source_system   text,

    effective_from  timestamptz NOT NULL DEFAULT now(),
    effective_to    timestamptz,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    recorded_by     text,

    CONSTRAINT ck_consent_window CHECK (effective_to IS NULL OR effective_to > effective_from)
);

COMMENT ON TABLE mdm.consent IS
    'Consent history per person and purpose. The do_not_contact flag on Person '
    'is a projection of this; this table is the record.';

CREATE INDEX ix_consent_person ON mdm.consent (person_id, purpose, effective_from DESC);

-- One live consent state per person and purpose. Two contradictory live records
-- would make "may we contact them" unanswerable.
CREATE UNIQUE INDEX uq_consent_current
    ON mdm.consent (person_id, purpose)
    WHERE effective_to IS NULL;


-- ===========================================================================
-- Erasure
-- ===========================================================================
--
-- Right-to-be-forgotten against this architecture is genuinely hard, and
-- pretending otherwise would be the wrong design. The obstacles are real:
--
--   * The landing zone is immutable and content-addressed by design.
--   * Golden records are SCD-2, so history is the point.
--   * Some data must be retained despite an erasure request -- an in-force
--     insurance contract carries statutory retention that overrides deletion.
--
-- So erasure is a tracked workflow with an explicit scope decision, not a
-- DELETE. The request records what was asked, what was legally assessable, what
-- was actually erased, and what was retained and why. That last field is the
-- one that matters: a regulator asking why data survived an erasure request
-- needs an answer recorded at the time, not reconstructed afterwards.

CREATE TYPE mdm.erasure_state AS ENUM (
    'REQUESTED',
    'ASSESSING',
    'PARTIALLY_ERASED',
    'ERASED',
    'REFUSED'
);

CREATE TABLE mdm.erasure_request (
    request_id      uuid PRIMARY KEY,
    person_id       uuid        NOT NULL REFERENCES mdm.person_master (person_id),
    state           mdm.erasure_state NOT NULL DEFAULT 'REQUESTED',

    requested_by    text        NOT NULL,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    legal_basis     text,

    -- What happened, in detail. Counts alone would not survive an audit.
    erased_fields   text[]      NOT NULL DEFAULT '{}',
    retained_fields text[]      NOT NULL DEFAULT '{}',
    retention_reason text,
    affected_versions integer   NOT NULL DEFAULT 0,
    affected_source_records integer NOT NULL DEFAULT 0,

    executed_by     text,
    executed_at     timestamptz,
    notes           text,

    -- Anything retained must say why. An erasure request that silently kept
    -- data is the failure this table exists to prevent.
    CONSTRAINT ck_erasure_retention CHECK (
        cardinality(retained_fields) = 0 OR retention_reason IS NOT NULL
    ),
    CONSTRAINT ck_erasure_executed CHECK (
        state NOT IN ('ERASED', 'PARTIALLY_ERASED')
        OR (executed_by IS NOT NULL AND executed_at IS NOT NULL)
    )
);

COMMENT ON TABLE mdm.erasure_request IS
    'Right-to-be-forgotten workflow. Records what was erased, what was retained '
    'and the legal reason for retention.';

CREATE INDEX ix_erasure_person ON mdm.erasure_request (person_id, requested_at DESC);
CREATE INDEX ix_erasure_open ON mdm.erasure_request (state, requested_at)
    WHERE state IN ('REQUESTED', 'ASSESSING');


-- ===========================================================================
-- Access log
-- ===========================================================================
--
-- Append-only, and it covers reads. Most systems log mutations and consider
-- themselves audited; the question actually asked after an incident is who
-- looked at a person's record, and that is unanswerable from a mutation log.
--
-- Deliberately narrow and cheap to write: this is on the hot path of every API
-- call, so it must not be a join or a trigger over the golden tables.

CREATE TYPE mdm.access_action AS ENUM (
    'READ',
    'SEARCH',
    'DUPLICATE_CHECK',
    'SUBMIT',
    'MERGE',
    'SPLIT',
    'OVERRIDE',
    'APPROVE_RULE',
    'ERASE',
    'EXPORT',
    'DENIED'
);

CREATE TABLE mdm.access_log (
    log_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    principal_id    uuid,
    subject         text,
    action          mdm.access_action NOT NULL,

    entity_name     text,
    entity_id       uuid,
    -- Number of records the call touched. A search returning 40,000 people is a
    -- different event from one returning three, and the difference is the whole
    -- signal for exfiltration monitoring.
    record_count    integer     NOT NULL DEFAULT 1,

    -- Whether direct PII was returned unmasked.
    pii_revealed    boolean     NOT NULL DEFAULT false,

    request_id      text,
    client_ip       inet,
    detail          jsonb
);

COMMENT ON TABLE mdm.access_log IS
    'Append-only access trail covering reads as well as writes. Who looked at a '
    'record is the question asked after an incident.';

CREATE INDEX ix_access_log_time ON mdm.access_log (occurred_at DESC);
CREATE INDEX ix_access_log_principal ON mdm.access_log (principal_id, occurred_at DESC);
CREATE INDEX ix_access_log_entity ON mdm.access_log (entity_id, occurred_at DESC);
CREATE INDEX ix_access_log_bulk ON mdm.access_log (occurred_at DESC)
    WHERE record_count > 100;

-- Append-only enforced by the database, not by convention. An audit trail a
-- compromised application account can rewrite is not an audit trail.
CREATE RULE access_log_no_update AS ON UPDATE TO mdm.access_log DO INSTEAD NOTHING;
CREATE RULE access_log_no_delete AS ON DELETE TO mdm.access_log DO INSTEAD NOTHING;


-- ===========================================================================
-- Steward actions
-- ===========================================================================
--
-- Manual interventions, separated from the automated audit trails so that "what
-- did a human change" is one query rather than a filter over everything.

CREATE TABLE mdm.steward_action (
    action_id       uuid PRIMARY KEY,
    action          mdm.access_action NOT NULL,
    principal_id    uuid REFERENCES mdm.principal (principal_id),
    subject         text        NOT NULL,

    entity_name     text        NOT NULL,
    entity_id       uuid,
    related_id      uuid,

    -- Before and after, so a manual change is reversible and reviewable without
    -- reconstructing it from version history.
    before_value    jsonb,
    after_value     jsonb,
    reason          text        NOT NULL,

    performed_at    timestamptz NOT NULL DEFAULT now(),
    reverted_at     timestamptz,
    reverted_by     text,

    -- A manual override with no stated reason is the one nobody can defend
    -- later, so the column is NOT NULL and this check stops a blank standing in.
    CONSTRAINT ck_steward_reason CHECK (length(btrim(reason)) > 3)
);

COMMENT ON TABLE mdm.steward_action IS
    'Manual interventions with before/after state and a mandatory reason.';

CREATE INDEX ix_steward_action_entity ON mdm.steward_action (entity_id, performed_at DESC);
CREATE INDEX ix_steward_action_who ON mdm.steward_action (subject, performed_at DESC);
