-- Standardization rules: rule kinds and extraction targets.
--
-- A new migration rather than an edit to 002. Migrations are immutable once
-- applied -- the database already ran the old text, so editing the file would
-- make it describe a schema that does not exist, and the checksum guard in the
-- migration runner exists precisely to stop that.

SET search_path TO mdm, public;

-- What a rule does when it fires.
--
-- REWRITE is the classic case the design calls for: a regex and a replacement,
-- applied to a text column. It cleans a value.
--
-- EXTRACT handles the case a rewrite cannot. Name component inference is not a
-- string cleanup -- it populates several derived columns from one input using
-- named capture groups. Without this kind, the largest category of quality-gate
-- failure could never be retired into the deterministic path, which would leave
-- the learning loop unable to do the one thing it exists for.
CREATE TYPE mdm.rule_kind AS ENUM (
    'REWRITE',
    'EXTRACT'
);

ALTER TABLE mdm.standardization_rule
    ADD COLUMN rule_kind mdm.rule_kind NOT NULL DEFAULT 'REWRITE';

-- For EXTRACT rules: which canonical columns the named capture groups populate.
-- Stored explicitly rather than inferred from the pattern so that a reviewer can
-- see what a rule will write without having to parse a regex in their head.
ALTER TABLE mdm.standardization_rule
    ADD COLUMN target_fields text[] NOT NULL DEFAULT '{}';

-- Which quality-gate check this rule is intended to retire. The loop reports
-- how much of the AI backlog each rule actually removes, which is only
-- measurable if the rule declares what it was aiming at.
ALTER TABLE mdm.standardization_rule
    ADD COLUMN targets_check text;

-- An EXTRACT rule that names no target columns would silently do nothing.
ALTER TABLE mdm.standardization_rule
    ADD CONSTRAINT ck_rule_extract_targets CHECK (
        rule_kind <> 'EXTRACT' OR cardinality(target_fields) > 0
    );

COMMENT ON COLUMN mdm.standardization_rule.rule_kind IS
    'REWRITE applies a regex replacement to a text column. EXTRACT populates '
    'target_fields from named capture groups.';
