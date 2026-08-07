# Customer MDM

Open-source, Python-native Customer Master Data Management for insurance data.

Vectorized ingestion and matching over Apache Arrow, ACID golden-record storage
in PostgreSQL, and local open-source models invoked only where the deterministic
and probabilistic passes abstain.

> **Status: canonical model, ingestion, standardization and identity
> resolution are built and measured. Survivorship, golden-record writer, APIs,
> UI, governance and observability are not yet built.**
>
> [`docs/01-canonical-data-model.md`](docs/01-canonical-data-model.md) — the three
> entities, identity strategy, assumptions taken, open questions.
> [`docs/02-vectorized-ingestion.md`](docs/02-vectorized-ingestion.md) — mapping,
> normalization kernels, shredding, measured throughput.

### Measured, not asserted

| Stage | Result |
|---|---|
| Shredding | 37,700 policies/s |
| Standardization | deterministic pass 85.3% → **100%** after one learning cycle; AI share 14.7% → **0%** |
| Blocking | 1,553× pair reduction, 99.2% blocking recall |
| Resolution | precision **0.991**, recall **0.780** vs generator ground truth |
| Grey-zone model | contributes **46% of all true positives at 100% precision** |

All figures from `data/life_admin_sample.csv` (5,000 policies, 3,881 parties),
against a real PostgreSQL 16 instance. Reproduce with
`python -m scripts.generate_sample_data`.

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
src/cmdm/ingest/
    mapping.py      Declarative source → canonical mapping, registry-validated
    normalize.py    Vectorized kernels — every function returns a Polars expression
    shred.py        Policy grain → Policy / Person / Relationship
src/cmdm/db/
    engine.py       Pooled connections, forward-only migrations
    queue.py        Postgres work queue (FOR UPDATE SKIP LOCKED)
src/cmdm/standardize/
    gate.py         Declared quality checks — decides who ever sees a model
    rules.py        Learned rule store, gated PROPOSED→SHADOW→APPROVED→ACTIVE
    ai.py           Local AI fallback (ONNX + heuristic reference)
    agent.py        Pattern mining, rule proposal, shadow evaluation
    pipeline.py     The four stages wired together
src/cmdm/resolve/
    blocking.py     Candidate generation from the ingest-time keys
    scoring.py      Vectorized comparators, tri-zone split, vetoes
    crossencoder.py Grey-zone classifier (ONNX + feature reference)
    clustering.py   SciPy connected components → master ids
    pipeline.py     Resolution run, persisted with its thresholds
src/cmdm/mappings/
    life_admin.toml Example source mapping
src/cmdm/sql/
    001_golden_schema.sql   Generated from the registry. Do not edit.
    002_pipeline.sql        Queue, batches, rules, match ledger
    003_standardization.sql Rule kinds and extraction targets
docs/
    01-canonical-data-model.md
    02-vectorized-ingestion.md
```

## Getting started

```bash
pip install -e ".[dev,vector]"
python -m scripts.render_ddl                       # regenerate the DDL from the registry
python -m scripts.generate_sample_data --rows 5000 # synthetic extract with real-world mess
pytest                                             # 148 tests
```

Shredding a sample extract:

```python
import polars as pl
from cmdm.ingest.mapping import load_mapping
from cmdm.ingest.shred import shred

mapping = load_mapping("src/cmdm/mappings/life_admin.toml")
raw = pl.read_csv("data/life_admin_sample.csv", infer_schema_length=0)
frames = shred(raw, mapping)   # {"policy": ..., "person": ..., "relationship": ...}
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
- **Normalization is expressions, not loops.** No `map_elements` anywhere in the
  ingest path — 37,700 policies/s on the reference batch.
- **Deterministic collapse runs before blocking.** An agent appears on every
  policy they wrote; blocking the uncollapsed frame does quadratic work to
  rediscover what the source already stated. Collapsing first cuts candidate
  pairs 26×.

## License

Apache-2.0.
