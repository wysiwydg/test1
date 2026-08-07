-- Provenance may name no source record.
--
-- winning_source_record_id was declared NOT NULL on the assumption that every
-- surviving value comes from a landed row. Two legitimate cases break that:
--
--   * A steward override. The value was asserted by a human, not a feed, and
--     there is no source record to point at -- but the provenance row is more
--     important here than anywhere else, because a manual value is exactly the
--     one somebody will later ask about.
--   * A survivorship pass over records assembled outside ingestion, such as a
--     backfill or a replay from an archive.
--
-- Forcing a synthetic record id for those would put a lie in the audit trail.
-- The column becomes nullable and the strategy column carries the explanation.

SET search_path TO mdm, public;

ALTER TABLE mdm.attribute_provenance
    ALTER COLUMN winning_source_record_id DROP NOT NULL;

ALTER TABLE mdm.attribute_provenance
    ALTER COLUMN winning_source_system DROP NOT NULL;

-- A provenance row must still say where the value came from in one form or
-- another: either a source record, or a named source system, or a strategy that
-- inherently has no single contributor (ANY_TRUE aggregates across all of them).
ALTER TABLE mdm.attribute_provenance
    ADD CONSTRAINT ck_provenance_attribution CHECK (
        winning_source_record_id IS NOT NULL
        OR winning_source_system IS NOT NULL
        OR strategy IN ('ANY_TRUE', 'AGGREGATE_MAX', 'AGGREGATE_MIN', 'DERIVED', 'SYSTEM')
    );

COMMENT ON COLUMN mdm.attribute_provenance.winning_source_record_id IS
    'Source record the surviving value came from. Null for steward overrides and '
    'for aggregate strategies that have no single contributing record.';
