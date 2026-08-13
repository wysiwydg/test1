-- Households and affiliations.
--
-- HAND-WRITTEN, like 002-006, and idempotent on purpose.
--
-- 001 is generated from the field registry, so a store created today already
-- has these columns and this enum. A store created last week does not, and
-- migrations are forward-only and applied in order -- so this file has to be a
-- no-op on the first kind and a real change on the second. Every statement is
-- therefore IF NOT EXISTS, which is the whole reason it can be applied to both.
--
-- What is added, and why the model needed it:
--
-- The canonical model has always declared PARTY_PARTY edges, an association
-- vocabulary and a HOUSEHOLD_MEMBER value. Nothing derived any of them, so the
-- question "who is in this customer's household, and do they belong to a
-- company or a trust" had a schema to answer it and no data. Three of the
-- pieces were missing.
--
-- `stated_relationship` holds what the source said about the owner's relation
-- to the life insured. It is not derived and does not belong in the association
-- vocabulary: insurable interest is a condition of issue, so a life
-- administration system captures SPOUSE or CHILD or EMPLOYER at application,
-- and that assertion is stronger evidence than any inference from surnames and
-- postcodes. Storing it on the sourced edge keeps the derivation recomputable
-- from the golden store rather than only from a re-shred of the landing zone.
--
-- `household_id` and `household_size` are denormalised onto Person because
-- "everyone in this customer's household" is asked on every servicing screen,
-- and answering it by walking a graph would make the common case the expensive
-- one. `affiliation_count` is kept separate from `household_size` on purpose: a
-- company insuring forty staff is not a household of forty-one, and collapsing
-- the two is the standard way a householding feature produces nonsense.

ALTER TABLE mdm.person
    ADD COLUMN IF NOT EXISTS household_id uuid,
    ADD COLUMN IF NOT EXISTS household_size integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS affiliation_count integer NOT NULL DEFAULT 0;

-- The registry declares these NOT NULL with no default; the default above only
-- exists so that adding the column to a populated table succeeds. Dropping it
-- afterwards keeps the two definitions identical, so a store migrated to here
-- and a store created from 001 today are the same schema.
ALTER TABLE mdm.person
    ALTER COLUMN household_size DROP DEFAULT,
    ALTER COLUMN affiliation_count DROP DEFAULT;

CREATE INDEX IF NOT EXISTS ix_person_household_id
    ON mdm.person (household_id)
    WHERE is_current;

ALTER TABLE mdm.relationship
    ADD COLUMN IF NOT EXISTS stated_relationship text;

CREATE INDEX IF NOT EXISTS ix_relationship_stated_relationship
    ON mdm.relationship (stated_relationship)
    WHERE is_current;

-- The association vocabulary gains the familial and affiliation kinds. The
-- structural ones -- CO_INSURED and friends -- were already there; they say two
-- parties share a policy, which is true by construction and tells you nothing
-- about how they are related.
--
-- ADD VALUE IF NOT EXISTS is not transactional before PostgreSQL 12 and cannot
-- run inside a transaction block on some versions; from 12 onwards it can, and
-- the engine applies each migration in one. 16 is the floor this project ships.
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'SPOUSE_OF';
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'CHILD_OF';
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'PARENT_OF';
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'EMPLOYEE_OF';
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'TRUST_MEMBER_OF';
ALTER TYPE mdm.association_type ADD VALUE IF NOT EXISTS 'ESTATE_SUBJECT_OF';
