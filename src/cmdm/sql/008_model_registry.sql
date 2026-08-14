-- The model registry.
--
-- HAND-WRITTEN, like 002-007, and idempotent for the same reason.
--
-- Two local models decide things: the standardization fallback and the
-- grey-zone classifier. Which one ran was already recorded on every decision --
-- `match_pair.model_name` and `standardization_exception` carry it -- so a past
-- decision has always been explainable. What could not be answered is the
-- question asked *before* a decision: which model should be running, on what
-- evidence, and who said so.
--
-- Until now that was an environment variable. `CMDM_STANDARDIZER_MODEL` points
-- at a file; whatever is at that path runs. Swapping it is a deployment action
-- with no record, no measurement and no approval -- which is exactly the shape
-- of change this project refuses everywhere else. A standardization *rule* has
-- to be mined, shadow-tested against a regression set and approved by a steward
-- with a reason before it can touch a record. A model that rewrites the same
-- fields, and decides which parties are the same person, had none of that.
--
-- So a model version goes through the states a rule goes through, for the same
-- reason and with the same refusal: the database will not hold an ACTIVE model
-- that was never evaluated.
--
--     CANDIDATE -> SHADOW -> ACTIVE
--                     |
--                  RETIRED
--
-- The metrics column holds whatever the evaluation produced -- precision,
-- recall, F1, blocking recall, the counts behind them. Stored as delivered
-- rather than as columns because what is worth measuring differs by model kind,
-- and a schema that fixed the list would have to change every time a new
-- measurement turned out to matter.

CREATE TABLE IF NOT EXISTS mdm.model_version (
    model_id        uuid PRIMARY KEY,
    kind            text NOT NULL,
    model_name      text NOT NULL,
    version         text NOT NULL,
    state           text NOT NULL DEFAULT 'CANDIDATE',
    artifact_path   text,
    artifact_sha256 text,

    -- Evidence. Null until an evaluation has run.
    evaluated_at    timestamptz,
    evaluated_on    text,
    metrics         jsonb,

    -- Who accepted the evidence.
    promoted_at     timestamptz,
    promoted_by     text,
    promotion_note  text,

    registered_at   timestamptz NOT NULL DEFAULT now(),
    registered_by   text NOT NULL DEFAULT 'system',

    CONSTRAINT ck_model_kind CHECK (
        kind IN ('STANDARDIZER', 'CROSS_ENCODER')
    ),
    CONSTRAINT ck_model_state CHECK (
        state IN ('CANDIDATE', 'SHADOW', 'ACTIVE', 'RETIRED')
    ),
    -- The refusal that makes the rest of it mean something. A model cannot be
    -- ACTIVE without a recorded evaluation and a named human, in exactly the
    -- way an ACTIVE standardization rule cannot exist without a shadow result.
    CONSTRAINT ck_model_active_was_reviewed CHECK (
        state <> 'ACTIVE'
        OR (metrics IS NOT NULL AND evaluated_at IS NOT NULL
            AND promoted_by IS NOT NULL)
    )
);

COMMENT ON TABLE mdm.model_version IS 'Registered local models, their measured quality, and who approved each one for use. A model reaches ACTIVE only with evidence and a named approver.';
COMMENT ON COLUMN mdm.model_version.kind IS 'Which decision this model makes: STANDARDIZER rewrites field values the deterministic pass could not parse; CROSS_ENCODER judges grey-zone pairs.';
COMMENT ON COLUMN mdm.model_version.artifact_sha256 IS 'Digest of the model file as registered. A model whose artifact has changed on disk is not the model that was evaluated, and the digest is what makes that detectable rather than assumed.';
COMMENT ON COLUMN mdm.model_version.evaluated_on IS 'What the metrics were measured against — the extract and its parameters. A precision figure without the corpus it came from is not comparable to anything.';
COMMENT ON COLUMN mdm.model_version.metrics IS 'The evaluation result as produced. Shape differs by kind, so it is stored as delivered rather than flattened into columns that would need changing whenever a new measurement matters.';
COMMENT ON COLUMN mdm.model_version.promotion_note IS 'Why this model was accepted. The rule store requires the same, because an approval with no reason is not reviewable six months later.';

-- Exactly one ACTIVE model per kind. Two active standardizers is not a
-- configuration, it is a race between whichever loads first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_active_per_kind
    ON mdm.model_version (kind)
    WHERE state = 'ACTIVE';

CREATE UNIQUE INDEX IF NOT EXISTS uq_model_name_version
    ON mdm.model_version (kind, model_name, version);

CREATE INDEX IF NOT EXISTS ix_model_state
    ON mdm.model_version (state);
