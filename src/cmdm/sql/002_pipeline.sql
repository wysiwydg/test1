-- Pipeline infrastructure.
--
-- HAND-WRITTEN, unlike 001_golden_schema.sql.
--
-- 001 is the canonical data model: it is generated from the field registry and
-- a test fails if the two drift. These tables are not part of the canonical
-- model. They are operational machinery -- a work queue, batch bookkeeping, the
-- standardization rule store, the match-pair ledger -- and modelling them as
-- canonical entities would put queue plumbing into the registry that describes
-- what a Person is. They are versioned as ordinary migrations instead.
--
-- Everything here still obeys the same rules as the golden store: no value is
-- overwritten where its history is interesting, every AI decision is logged with
-- enough context to reproduce it, and the schema constrains what the writers are
-- allowed to do rather than trusting them.

SET search_path TO mdm, public;


-- ===========================================================================
-- Work queue
-- ===========================================================================
--
-- Ingestion is decoupled from processing by this table. It is in the same
-- database as the landing zone deliberately: accepting a batch means landing
-- the raw records AND obliging something to process them, and putting the queue
-- here makes those one transaction. A broker in another process makes them two,
-- which is how a batch becomes accepted-but-lost when the second one fails.

CREATE TYPE mdm.job_state AS ENUM (
    'PENDING',
    'RUNNING',
    'DONE',
    'FAILED',
    'DEAD'
);

CREATE TABLE mdm.work_queue (
    job_id          uuid PRIMARY KEY,
    queue_name      text        NOT NULL,
    payload         jsonb       NOT NULL,
    priority        integer     NOT NULL DEFAULT 100,
    state           mdm.job_state NOT NULL DEFAULT 'PENDING',
    attempts        integer     NOT NULL DEFAULT 0,
    max_attempts    integer     NOT NULL DEFAULT 5,

    -- Visibility deadline, not merely a lock. A worker that dies releases its
    -- database lock instantly, but the job must not become claimable that same
    -- instant -- the work may still be in flight. This gives it a defined
    -- window instead, and makes a crashed worker self-healing.
    visible_at      timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_at       timestamptz,

    -- Idempotent enqueue. A retried API call or a file delivered twice must not
    -- queue the same batch twice.
    dedupe_key      text,

    result          jsonb,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,

    CONSTRAINT ck_work_queue_attempts CHECK (attempts <= max_attempts + 1)
);

COMMENT ON TABLE mdm.work_queue IS
    'At-least-once work queue claimed with FOR UPDATE SKIP LOCKED. Retained '
    'after completion: the queue doubles as the processing audit trail.';

-- The claim query's access path. Ordering matches the ORDER BY so a claim is an
-- index scan of exactly the rows it will take, not a sort of the whole queue.
CREATE INDEX ix_work_queue_claim
    ON mdm.work_queue (queue_name, priority, visible_at)
    WHERE state IN ('PENDING', 'FAILED');

-- Dedupe applies only while a job is live. Once it is DONE the same key may be
-- enqueued again, which is what allows a batch to be deliberately reprocessed.
CREATE UNIQUE INDEX uq_work_queue_dedupe
    ON mdm.work_queue (queue_name, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('PENDING', 'RUNNING');

CREATE INDEX ix_work_queue_lease
    ON mdm.work_queue (queue_name, visible_at)
    WHERE state = 'RUNNING';

CREATE INDEX ix_work_queue_dead
    ON mdm.work_queue (queue_name, updated_at)
    WHERE state = 'DEAD';


-- ===========================================================================
-- Ingest batch
-- ===========================================================================
--
-- One row per accepted submission. Records the validation verdict, the counts,
-- and the content hash of the file, so that "did this file arrive, was it
-- accepted, what happened to it" is answerable without reading logs.

CREATE TYPE mdm.batch_state AS ENUM (
    'RECEIVED',
    'VALIDATED',
    'REJECTED',
    'LANDED',
    'STANDARDIZED',
    'RESOLVED',
    'COMPLETED',
    'FAILED'
);

CREATE TABLE mdm.ingest_batch (
    batch_id            uuid PRIMARY KEY,
    source_system       text        NOT NULL,
    mapping_name        text        NOT NULL,
    origin              text        NOT NULL,
    filename            text,

    -- Content hash of the submitted payload. Re-delivery of a byte-identical
    -- file is detectable here rather than after it has been shredded.
    content_hash        text        NOT NULL,

    state               mdm.batch_state NOT NULL DEFAULT 'RECEIVED',
    row_count           integer     NOT NULL DEFAULT 0,
    accepted_count      integer     NOT NULL DEFAULT 0,
    rejected_count      integer     NOT NULL DEFAULT 0,

    -- Structured validation findings. Kept whole rather than summarized to a
    -- message, so a rejected file can be diagnosed column by column.
    validation_report   jsonb,

    submitted_by        text,
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    last_error          text,

    CONSTRAINT ck_ingest_batch_counts CHECK (
        accepted_count >= 0 AND rejected_count >= 0
        AND accepted_count + rejected_count <= row_count
    )
);

COMMENT ON TABLE mdm.ingest_batch IS
    'One row per accepted submission, with the validation verdict and counts.';

CREATE INDEX ix_ingest_batch_source ON mdm.ingest_batch (source_system, submitted_at DESC);
CREATE INDEX ix_ingest_batch_state ON mdm.ingest_batch (state, submitted_at DESC);

-- A byte-identical redelivery is a no-op, not a second batch. Scoped to the
-- source so two systems may legitimately send identical files.
CREATE UNIQUE INDEX uq_ingest_batch_content
    ON mdm.ingest_batch (source_system, content_hash)
    WHERE state <> 'REJECTED';


-- ===========================================================================
-- Standardization rules
-- ===========================================================================
--
-- The rule store behind the learning loop. When the AI fallback handles the
-- same shape of edge case repeatedly, a proposed deterministic rule is written
-- here with the evidence that motivated it.
--
-- A proposal is NOT live. It is shadow-evaluated against the corpus it claims
-- to handle plus a regression set it must not break, and a steward approves it
-- before it takes effect. Normalization that rewrites itself into production
-- unreviewed can silently corrupt every record it touches, and the corruption
-- is uniform, which is the hardest kind to notice.

CREATE TYPE mdm.rule_state AS ENUM (
    'PROPOSED',
    'SHADOW',
    'APPROVED',
    'ACTIVE',
    'REJECTED',
    'RETIRED'
);

CREATE TABLE mdm.standardization_rule (
    rule_id             uuid PRIMARY KEY,
    field_name          text        NOT NULL,
    rule_name           text        NOT NULL,

    -- The deterministic rule itself: a regex and its replacement, applied as a
    -- vectorized Polars expression. Deliberately not arbitrary code -- a
    -- generated rule must be inspectable by a reviewer who is not a programmer,
    -- and must not be able to do anything but rewrite a string.
    pattern             text        NOT NULL,
    replacement         text        NOT NULL DEFAULT '',
    applies_to          text        NOT NULL DEFAULT 'ALL',

    state               mdm.rule_state NOT NULL DEFAULT 'PROPOSED',
    priority            integer     NOT NULL DEFAULT 100,

    -- Why this rule exists: the AI outputs that motivated it, and how many
    -- distinct records showed the pattern.
    evidence_count      integer     NOT NULL DEFAULT 0,
    evidence_sample     jsonb,
    proposed_by         text        NOT NULL,
    model_name          text,

    -- Shadow evaluation. Populated before approval is possible.
    shadow_tested_at    timestamptz,
    shadow_matched      integer,
    shadow_fixed        integer,
    shadow_regressions  integer,
    shadow_report       jsonb,

    reviewed_by         text,
    reviewed_at         timestamptz,
    review_note         text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- A rule cannot be active without having been reviewed. This is the
    -- human-approval gate expressed as a database constraint rather than as a
    -- convention the promotion code is trusted to follow.
    CONSTRAINT ck_rule_approval CHECK (
        state <> 'ACTIVE' OR (reviewed_by IS NOT NULL AND shadow_tested_at IS NOT NULL)
    ),
    -- A rule cannot be approved without evidence that it was shadow-tested and
    -- broke nothing.
    CONSTRAINT ck_rule_shadow CHECK (
        state NOT IN ('APPROVED', 'ACTIVE')
        OR (shadow_regressions IS NOT NULL AND shadow_regressions = 0)
    )
);

COMMENT ON TABLE mdm.standardization_rule IS
    'Deterministic rules, including those proposed by the AI agent from '
    'recurring edge cases. ACTIVE requires shadow evaluation and human review, '
    'enforced by check constraint.';

CREATE UNIQUE INDEX uq_standardization_rule_name
    ON mdm.standardization_rule (field_name, rule_name);

CREATE INDEX ix_standardization_rule_active
    ON mdm.standardization_rule (field_name, priority)
    WHERE state = 'ACTIVE';

CREATE INDEX ix_standardization_rule_queue
    ON mdm.standardization_rule (state, created_at DESC);


-- ===========================================================================
-- Standardization exceptions
-- ===========================================================================
--
-- Records that failed the quality gate, and what the AI fallback made of them.
-- This is both the audit trail for the AI path and the corpus the rule-proposing
-- agent mines: a pattern is only worth a deterministic rule if it recurs, and
-- recurrence is only visible if every exception is kept.

CREATE TABLE mdm.standardization_exception (
    exception_id        uuid PRIMARY KEY,
    batch_id            uuid,
    source_record_id    uuid,
    field_name          text        NOT NULL,

    -- The value that failed, and which gate rejected it.
    raw_value           text,
    deterministic_value text,
    failed_checks       text[]      NOT NULL,

    -- What the model produced, kept verbatim alongside the identifiers needed
    -- to reproduce it.
    ai_value            text,
    ai_confidence       double precision,
    model_name          text,
    model_version       text,
    prompt_hash         text,
    model_output        jsonb,
    latency_ms          integer,

    -- Set once a proposed rule claims to handle this exception, so the loop can
    -- report how much of the AI backlog each rule actually retires.
    covered_by_rule_id  uuid REFERENCES mdm.standardization_rule (rule_id),

    created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE mdm.standardization_exception IS
    'Quality-gate failures and the AI fallback output for each. Doubles as the '
    'corpus the rule-proposing agent mines for recurring patterns.';

CREATE INDEX ix_std_exception_field ON mdm.standardization_exception (field_name, created_at DESC);
CREATE INDEX ix_std_exception_batch ON mdm.standardization_exception (batch_id);
CREATE INDEX ix_std_exception_uncovered
    ON mdm.standardization_exception (field_name)
    WHERE covered_by_rule_id IS NULL;


-- ===========================================================================
-- Match pairs
-- ===========================================================================
--
-- The tri-zone ledger. Every candidate pair the blocking pass produced, its
-- composite score, which zone it fell into, and -- for grey-zone pairs -- what
-- the cross-encoder concluded.
--
-- Auto-rejected pairs are recorded too. A duplicate that reaches production is
-- investigated by asking why the pair was compared and rejected, which is
-- unanswerable if only the matches are kept.

CREATE TYPE mdm.match_zone AS ENUM (
    'AUTO_MATCH',
    'GREY',
    'AUTO_REJECT'
);

CREATE TABLE mdm.match_pair (
    pair_id             uuid PRIMARY KEY,
    run_id              uuid        NOT NULL,

    -- Ordered so that (left, right) and (right, left) cannot both exist.
    left_person_id      uuid        NOT NULL,
    right_person_id     uuid        NOT NULL,

    blocking_key        text,
    score               double precision NOT NULL,
    zone                mdm.match_zone NOT NULL,

    -- Per-comparator contributions, so a composite score is explainable and not
    -- merely reportable.
    comparator_scores   jsonb,

    -- Cross-encoder verdict, populated only for grey-zone pairs.
    ai_score            double precision,
    ai_decision         text,
    model_name          text,
    model_version       text,
    latency_ms          integer,

    final_decision      text        NOT NULL,
    decided_by          text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_match_pair_order CHECK (left_person_id < right_person_id),
    CONSTRAINT ck_match_pair_score CHECK (score >= 0.0 AND score <= 1.0),
    -- Only grey-zone pairs may carry a model verdict. If an auto-match or
    -- auto-reject has one, the zone routing has a bug and this catches it at
    -- write time rather than in a quarterly audit.
    CONSTRAINT ck_match_pair_ai_zone CHECK (
        ai_score IS NULL OR zone = 'GREY'
    )
);

COMMENT ON TABLE mdm.match_pair IS
    'Every candidate pair with its score, zone and decision. Non-matches are '
    'retained: a duplicate in production is diagnosed by asking why its pair '
    'was rejected.';

CREATE UNIQUE INDEX uq_match_pair_run
    ON mdm.match_pair (run_id, left_person_id, right_person_id);
CREATE INDEX ix_match_pair_zone ON mdm.match_pair (run_id, zone);
CREATE INDEX ix_match_pair_left ON mdm.match_pair (left_person_id);
CREATE INDEX ix_match_pair_right ON mdm.match_pair (right_person_id);


-- ===========================================================================
-- Resolution run
-- ===========================================================================
--
-- One row per identity-resolution pass, holding the thresholds it ran with.
-- Thresholds change as the model is tuned, so a decision is only interpretable
-- alongside the configuration that produced it.

CREATE TABLE mdm.resolution_run (
    run_id              uuid PRIMARY KEY,
    auto_match_threshold  double precision NOT NULL,
    auto_reject_threshold double precision NOT NULL,
    comparator_weights  jsonb       NOT NULL,
    model_name          text,
    model_version       text,

    candidate_pairs     bigint      NOT NULL DEFAULT 0,
    auto_match_count    bigint      NOT NULL DEFAULT 0,
    grey_count          bigint      NOT NULL DEFAULT 0,
    auto_reject_count   bigint      NOT NULL DEFAULT 0,
    ai_approved_count   bigint      NOT NULL DEFAULT 0,
    cluster_count       bigint      NOT NULL DEFAULT 0,

    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    duration_ms         integer,

    CONSTRAINT ck_resolution_run_thresholds CHECK (
        auto_reject_threshold < auto_match_threshold
    )
);

COMMENT ON TABLE mdm.resolution_run IS
    'One row per resolution pass with the thresholds and weights it used. A '
    'match decision is only interpretable alongside its configuration.';

CREATE INDEX ix_resolution_run_started ON mdm.resolution_run (started_at DESC);
