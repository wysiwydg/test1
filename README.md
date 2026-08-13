# Customer MDM

Open-source, Python-native Customer Master Data Management for insurance data.

Vectorized ingestion and matching over Apache Arrow, ACID golden-record storage
in PostgreSQL, and local open-source models invoked only where the deterministic
and probabilistic passes abstain.

> **Status: all ten steps built, tested against real PostgreSQL 16.**
>
> [`docs/01-canonical-data-model.md`](docs/01-canonical-data-model.md) — the three
> entities, identity strategy, assumptions taken, open questions.
> [`docs/02-vectorized-ingestion.md`](docs/02-vectorized-ingestion.md) — mapping,
> normalization kernels, shredding, measured throughput.
> [`docs/03-architecture.md`](docs/03-architecture.md) — the storage model, the
> runtime path, and exactly where the two local models are reached from.
> [`docs/04-operating-the-consoles.md`](docs/04-operating-the-consoles.md) — how to
> run it: ingestion, stewardship and the business view, with the setup each needs.
> [`docs/05-architecture-in-plain-language.md`](docs/05-architecture-in-plain-language.md)
> — the same architecture for a business audience, with no code in it.

### Measured, not asserted

| Stage | Result |
|---|---|
| Shredding | 22,400 policies/s, one wide row → three grains |
| Standardization | deterministic pass **86.4%**, gate pass **99.1%** after the model, AI share **13.0%** |
| Blocking | 2.9M possible pairs → 11,787 candidates, **248× reduction** |
| Grey zone | **1.05%** of candidate pairs reach a model; 41 merged, 83 held apart |
| Vetoes | 1,789 pairs refused on conflicting DOB or person-vs-entity, whatever they scored |
| Full pipeline | 5,000 policies → 2,386 golden persons, 15,000 edges, 598 households in **~5 s**, one transaction |
| Re-processing | writes **0 changes**, 22,386 rows unchanged |
| Householding | 598 households, **every one a single real family**, 0 flatmates wrongly included, 80.3% of real families found |
| Audit | 11,787 pair decisions and 314 model invocations retained, **including every rejection** |

All figures from `data/life_admin_sample.csv` (5,000 policies, 2,419 source
identities), against a real PostgreSQL 16 instance. Reproduce with
`python -m scripts.generate_sample_data` then `./verify.sh`.

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

**Households** are derived on top of those edges, from the relationship the
source stated at application — insurable interest is a condition of issue, so a
life admin system records that the owner is the insured's spouse, parent or
child. Deliberately *not* from shared addresses: two people at one postcode are
two people. A party's links to companies, trusts and estates are counted
separately, because an employer is not a family.

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
src/cmdm/survive/
    engine.py       Registry-driven survivorship, one group-by, full lineage
src/cmdm/store/
    writer.py       SCD-2 golden writer, crosswalk, merge-pointer resolution
src/cmdm/governance/
    rbac.py         Roles, registry-driven PII masking, append-only access log
    privacy.py      Consent history, erasure workflow
src/cmdm/api/
    deps.py         Shared connection and authentication dependencies
    app.py          Read/search, submit, real-time duplicate check, /metrics
src/cmdm/ui/
    console.py      Ingestion, steward and business consoles, server-rendered
src/cmdm/export.py      Entity dashboard, and the delivered extract with MDM ids
src/cmdm/household.py   Households and affiliations, from stated relationships
src/cmdm/observe/
    metrics.py      Operational, match-quality and data-quality metrics
src/cmdm/pipeline.py    End-to-end orchestration
src/cmdm/worker.py      Queue drainer, batch processor, rule miner (CLI)
src/cmdm/mappings/
    life_admin.toml Example source mapping
src/cmdm/sql/
    001_golden_schema.sql   Generated from the registry. Do not edit.
    002_pipeline.sql        Queue, batches, rules, match ledger
    003_standardization.sql Rule kinds and extraction targets
    004_provenance_nullable.sql
    005_governance.sql      Principals, consent, erasure, audit
    006_match_pair_identity.sql  The ledger keys on what was compared
    007_households.sql      Households, affiliations, stated relationships
docs/
    01-canonical-data-model.md
    02-vectorized-ingestion.md
    03-architecture.md          Technical: storage, runtime path, the AI branches
    04-operating-the-consoles.md
    05-architecture-in-plain-language.md   The same, for a business audience
scripts/
    bootstrap.py            Migrations + one API key per console audience
```

## Getting started

```bash
pip install -e ".[dev,vector]"
python -m scripts.render_ddl                       # regenerate the DDL from the registry
python -m scripts.generate_sample_data --rows 5000 # synthetic extract with real-world mess
pytest                                             # 428 tests
```

Running the API and consoles:

```bash
export CMDM_DSN="host=/tmp port=5432 user=postgres dbname=cmdm"
export CMDM_ID_HASH_KEY="…"

python -m scripts.bootstrap             # migrate, and print one key per audience
uvicorn cmdm.api.app:app --port 8000    # API + all three consoles
python -m cmdm.worker serve             # drains the ingest queue
```

- `/console/login` — paste a key; it becomes an HttpOnly session cookie
- `/console/ingest` — submit a batch, watch it land, process the queue
- `/console` — business console: search, golden record, lineage
- `/console/entities` — what is in Person, Policy and Relationship, and a browser
- `/console/export` — the delivered extract back with MDM ids, or golden records
- `/console/steward` — grey-zone review queue
- `/console/rules` — learned-rule approvals (`python -m cmdm.worker mine` fills it)
- `/console/quality` — completeness and conformity
- `/docs` — OpenAPI
- `/metrics` — Prometheus

Full walkthrough: [`docs/04-operating-the-consoles.md`](docs/04-operating-the-consoles.md).

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
- **The queue lives in Postgres** so landing a batch and obliging something to
  process it are one transaction. A separate broker makes them two, and the gap
  is where a batch becomes accepted-but-lost.
- **Masking is driven by the registry's PII class**, so a new attribute is
  protected by declaring what it is, not by remembering a list.
- **The access log covers reads and refuses UPDATE and DELETE.** Who *looked* at
  a record is the question asked after an incident.
- **Erasure is a workflow, not a DELETE**, and records what it retained and why.
  A required column is tombstoned rather than skipped.
- **The console is server-rendered**, so a VIEWER's browser never receives the
  PII it is not allowed to see.
- **Golden ids come from the crosswalk, not from a fresh mint.** Re-processing a
  batch is meant to be routine — a steward decision only takes effect on the next
  run — so it has to be a no-op. Person has no natural-key index by design, so
  nothing in the database would have refused a second set of ids.
- **Every tie is broken deterministically, on a key that is actually unique.**
  One policy row yields an owner, an insured and an agent, so `source_record_id`
  alone ties; the identity triple does not. Without that, a threaded group-by
  picked a different winner each run and customers' names changed overnight.
- **A derived value comes from the record that won its parent.** `full_name` and
  `full_name_normalized` picked their winners independently, so 290 of 2,712
  records were searchable only under a name they did not display.
- **A steward's verdict is an input to the next run**, recorded as a fixed edge
  rather than applied to the store. The book does not change under whoever is
  reading it, and the review is spent once.

## License

Apache-2.0.
