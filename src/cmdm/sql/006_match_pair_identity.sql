-- The match ledger keys on the identities that were actually compared.
--
-- HAND-WRITTEN, like 002-005.
--
-- `match_pair` was declared in 002 with `left_person_id` / `right_person_id` as
-- NOT NULL uuids, which quietly assumed resolution compares golden persons. It
-- does not, and cannot: resolution runs on *source-scoped* identities -- a party
-- as one system knows it -- and the golden person_id only exists afterwards,
-- minted per cluster. The two id spaces are separated by exactly the stage this
-- table records.
--
-- The consequence was not a cosmetic mismatch. The end-to-end pipeline could not
-- persist a single row, so `match_pair` stayed empty, so the steward queue was
-- permanently empty and every resolution decision -- including the rejections
-- 002 exists to retain -- was discarded at the end of the run.
--
-- Nor is mapping to golden ids before writing a fix on its own. Every auto-match
-- collapses its two sides into one cluster and therefore one golden id, so the
-- pair would violate `left_person_id < right_person_id` and be rejected by the
-- very constraint that keeps a pair from being stored twice.
--
-- So the ledger is keyed on what was compared, and the golden ids become what
-- each side *landed in*: equal when the pair was merged, different when it was
-- not, null when resolution ran without a crosswalk (the duplicate-check path,
-- which resolves a candidate against the store without writing anything).

SET search_path TO mdm, public;


ALTER TABLE mdm.match_pair
    ADD COLUMN left_source_identity  text,
    ADD COLUMN right_source_identity text;

-- Rows written before this migration compared golden ids directly, so their
-- source identity is the id itself. Backfilled rather than left null so the
-- NOT NULL below holds for history as well as for new writes.
UPDATE mdm.match_pair
   SET left_source_identity  = left_person_id::text,
       right_source_identity = right_person_id::text;

ALTER TABLE mdm.match_pair
    ALTER COLUMN left_source_identity  SET NOT NULL,
    ALTER COLUMN right_source_identity SET NOT NULL,
    -- Null means "this run had no crosswalk", not "unknown". See above.
    ALTER COLUMN left_person_id  DROP NOT NULL,
    ALTER COLUMN right_person_id DROP NOT NULL;

COMMENT ON COLUMN mdm.match_pair.left_source_identity IS
    'The source-scoped identity compared, as resolution saw it. The ledger key.';
COMMENT ON COLUMN mdm.match_pair.left_person_id IS
    'The golden person this side landed in. Equal to the right side when the '
    'pair merged; null when the run had no crosswalk.';

-- Ordering moves to the identities that are now the key. Same purpose: (a, b)
-- and (b, a) are the same comparison and must not both exist.
ALTER TABLE mdm.match_pair DROP CONSTRAINT ck_match_pair_order;
ALTER TABLE mdm.match_pair ADD CONSTRAINT ck_match_pair_order
    CHECK (left_source_identity < right_source_identity);

DROP INDEX mdm.uq_match_pair_run;
CREATE UNIQUE INDEX uq_match_pair_run
    ON mdm.match_pair (run_id, left_source_identity, right_source_identity);

-- A steward's verdict is looked up by identity on the next run, over every run
-- ever recorded, so it needs its own access path rather than riding the
-- run-scoped unique index.
CREATE INDEX ix_match_pair_steward
    ON mdm.match_pair (left_source_identity, right_source_identity, created_at DESC)
    WHERE decided_by = 'STEWARD';
