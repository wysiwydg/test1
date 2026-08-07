# Customer MDM

Open-source, Python-native Customer Master Data Management for insurance data.

Vectorized ingestion and matching over Apache Arrow, ACID golden-record storage
in PostgreSQL, and local open-source models invoked only where the deterministic
and probabilistic passes abstain.

> **Status: step 1 of N — canonical data model.**
> See [`docs/01-canonical-data-model.md`](docs/01-canonical-data-model.md) for
> the design, the assumptions taken, and the open questions.

---

## The idea

Most MDM platforms are either fast or explainable. This one aims at both by
splitting the work by difficulty rather than running one engine over everything:

| Pass | Handles | Cost |
|---|---|---|
| **Deterministic** | Exact source-key and identifier matches | Hash join |
| **Vectorized probabilistic** | Blocking + comparator scoring over Arrow columns | Column kernels, no row loops |
| **Local AI fallback** | Only the undecided band the scorer abstains on | Proportional to genuine ambiguity, not to volume |

The point of the third row is that it stays small. `MatchDecision.REVIEW` is a
first-class outcome, so the scorer is allowed to say "I don't know" instead of
being forced into a binary call it can't support — and only those pairs reach a
model. Every AI decision is logged with its model, version, prompt hash and raw
output, so it's reproducible, reviewable in isolation, and revocable in bulk.

## Canonical model

Three entities:

- **Policy** — the insurance contract, and the grain the source data arrives in.
  Deterministic identity via policy number within a source system.
- **Person** — the party in a policy role, natural *or* legal (trusts, estates
  and companies own policies routinely). No natural key; identity lives in a
  crosswalk.
- **Relationship** — the role-bearing edge. Sourced Person→Policy edges carrying
  Owner / Insured / Agent, plus Person↔Person edges derived from them for
  householding and agent-book views.

Plus the control tables that make the above defensible: an immutable landing
zone, the identity crosswalk, per-attribute survivorship provenance, and an
append-only match audit.

## One source of truth

`src/cmdm/model/fields.py` declares every canonical attribute once, with its
type, survivorship rule, matching role and PII class. Everything else is
projected from it:

```
fields.py ──> ddl.py    ──> src/cmdm/sql/001_golden_schema.sql
          └─> arrow.py  ──> pyarrow.Schema / polars schema
```

The DDL is committed and a test regenerates it and fails on drift, so a field
added without regenerating breaks the build instead of quietly diverging.

## Layout

```
src/cmdm/model/
    enums.py        Controlled vocabularies (Arrow dictionaries, Postgres enums)
    fields.py       The field registry — single source of truth
    control.py      Landing zone, crosswalk, provenance, match audit
    ids.py          UUIDv7 keys, content hashing, keyed identifier hashing
    arrow.py        Arrow / Polars projection
    ddl.py          PostgreSQL projection
src/cmdm/sql/
    001_golden_schema.sql   Generated. Do not edit.
docs/
    01-canonical-data-model.md
```

## Getting started

```bash
pip install -e ".[dev]"
python -m scripts.render_ddl     # regenerate the DDL from the registry
pytest                           # 90 tests
```

Applying the schema:

```bash
createdb cmdm
psql cmdm -f src/cmdm/sql/001_golden_schema.sql
```

National identifier hashing requires a secret key — an unkeyed digest of a
nine-digit number is reversible by anyone with a laptop, so the code refuses to
run without one:

```bash
export CMDM_ID_HASH_KEY="…"   # never stored in the database
```

## Design notes worth knowing

- **Money is `decimal(18,4)`, never a float.** Premiums get reconciled against
  carrier ledgers; float drift makes those sums irreproducible.
- **Golden records are never updated in place.** SCD-2: a change closes the
  current version and inserts a new one. `record_hash` excludes audit columns,
  so re-ingesting unchanged data is a no-op.
- **Surrogate keys are UUIDv7**, not v4. Random keys scatter B-tree inserts
  across the whole index and turn every bulk load into a random-write workload;
  time-ordered keys append.
- **National identifiers never reach the golden record in the clear** — only a
  keyed BLAKE2b digest and the last four characters, which is all matching
  needs.
- **Suppression flags use `ANY_TRUE` survivorship.** An opt-out lost in a merge
  is a compliance breach, not a data-quality blemish.
- **Relationship separates system time from real-world time**, so a backdated
  agent-of-record change is representable.

## License

Apache-2.0.
