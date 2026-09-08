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
> [`docs/06-aml-and-transaction-monitoring.md`](docs/06-aml-and-transaction-monitoring.md)
> — AML and transaction monitoring for a Philippine insurance covered person:
> covered and suspicious transaction detection, name screening, CTR and STR
> filing with the AMLC.

### Measured, not asserted

| Stage | Result |
|---|---|
| Shredding | 22,400 policies/s, one wide row → three grains |
| Standardization | deterministic pass **86.3%**, gate pass **99.1%** after the model, AI share **13.0%** |
| Blocking | 2.9M possible pairs → 11,931 candidates, **243× reduction** |
| Grey zone | **0.81%** of candidate pairs reach a model; 18 merged, 79 held apart |
| Vetoes | 1,796 pairs refused on conflicting DOB or person-vs-entity, whatever they scored |
| Full pipeline | 5,000 policies → 2,389 golden persons, 15,000 edges, 597 households in **~4 s**, one transaction |
| Re-processing | writes **0 changes**, 22,389 rows unchanged |
| Householding | 597 households, **every one a single real family**, 0 flatmates wrongly included, 81.4% of real families found |
| Audit | 11,931 pair decisions and 314 model invocations retained, **including every rejection** |
| Match quality | on a benchmark with a known answer key: precision **0.94**, recall **0.72**, blocking recall **0.93** |

All figures from `data/life_admin_sample.csv` (5,000 policies, 2,407 source
identities), against a real PostgreSQL 16 instance. Reproduce with
`python -m scripts.generate_sample_data` then `./verify.sh`. Match quality is
measured separately, against an extract whose duplicates are known:
`python -m cmdm.worker evaluate --rows 2000 --duplicate-rate 0.18`.

**Footprint.** The source is about 1.5 MB and the built wheel 268 KB. What grows
is build output — bundles, packs, generated extracts, caches — none of which is
tracked. `python -m scripts.clean --dry-run` lists it; without the flag, it goes.
Everything that command removes is either git-ignored or reproducible by a
command it names.

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
src/cmdm/models.py      Model registry — which model may run, on what evidence
src/cmdm/evaluate.py    Match quality against known ground truth
src/cmdm/observe/
    metrics.py      Operational, match-quality and data-quality metrics
src/cmdm/pipeline.py    End-to-end orchestration
src/cmdm/worker.py      Queue drainer, batch processor, rule miner, evaluator (CLI)
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
    008_model_registry.sql  Registered models, their evidence, their approver
scripts/
    generate_sample_data.py  The reference extract, and its ground truth
    build_offline_bundle.py  The no-internet bundle
    build_update_pack.py     A small update for a bundle already installed
    clean.py                 Remove everything that can be rebuilt
src/aml/                AML and transaction monitoring (pure standard library)
    config.py           Thresholds, deadlines, screening, portal — the policy numbers
    money.py            Exact amounts, and the reference rate a filing depends on
    phcalendar.py       Philippine working days; the five-working-day clock
    model/              Vocabularies, AMLC code tables, the entities
    ingest/             Extract loading; the optional golden-record adapter
    rules/              14 detection rules, declarative and reproducible
    screening/          Name matching, World-Check, Dow Jones, local lists
    case/               Alert and case ledger, four-eyes workflow, audit chain
    report/             CTR and STR builders, renderers, validator
    report/specs/       The field layouts, as versioned data
    portal/             Deterministic packaging, spool and portal transports
    demo/               A synthetic book with an answer key
    console.py          The AML command line
docs/
    01-canonical-data-model.md
    02-vectorized-ingestion.md
    03-architecture.md          Technical: storage, runtime path, the AI branches
    04-operating-the-consoles.md
    05-architecture-in-plain-language.md   The same, for a business audience
    06-aml-and-transaction-monitoring.md   AML: detection, screening, CTR/STR filing
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
  `full_name_normalized` picked their winners independently, so 290 records
  were searchable only under a name they did not display.
- **A steward's verdict is an input to the next run**, recorded as a fixed edge
  rather than applied to the store. The book does not change under whoever is
  reading it, and the review is spent once.

---

# AML and Transaction Monitoring

A second product in this repository, built on the same customer data:
`src/aml` covers a Philippine insurance covered person's AML obligations —
detect covered and suspicious transactions, screen names against sanctions and
PEP lists, investigate under a four-eyes workflow, and file CTRs and STRs with
the AMLC.

Full documentation:
[`docs/06-aml-and-transaction-monitoring.md`](docs/06-aml-and-transaction-monitoring.md).

**Pure standard library.** No Arrow, no PostgreSQL, no HTTP client library, no
database driver. It is deployed into compliance units where every added
dependency is a security review, and it installs where it has to run. It works
standalone against a flat extract, and reads the golden customer record when the
MDM is deployed beside it — which is the difference between seeing one client
with eight policies and seeing eight unrelated clients.

### Measured, not asserted

From `python -m aml.console demo`, which writes a synthetic book with one
instance of each typology planted in a known place and an answer key beside it.

| Stage | Result |
|---|---|
| Monitoring | 14 rules over 454 transactions and 60 clients in **18 ms** |
| Detection | **10 of 10** planted typologies found |
| False positives | **0** alerts against the 50 ordinary premium payers |
| Covered transactions | 13, including a USD premium that only crosses the threshold once converted |
| Screening | 60 subjects, a designated-person match at 1.00, 0 false positives |
| Re-running | A second monitoring run creates **0** new alerts and updates 24 |
| Reports | 13 CTR rows and 1 STR row, 0 blocking validation issues |
| Determinism | Identical alert ids across runs; byte-identical submission packages |
| Audit | Hash-chained; an altered or deleted entry is detected and named |
| Tests | **99 passing in 2.3 s**, no database and no network |

### A working day, in commands

```bash
aml init                                    # a documented aml.toml
aml demo --dir data/aml-demo                # a synthetic book with an answer key
aml monitor --actor j.dizon                 # run the rules over last night's extract
aml screen  --actor j.dizon                 # screen the book against the lists
aml alerts --show                           # what came out, worst first, with narratives
aml case open --alert ALR-... --actor j.dizon          # opens with a drafted STR narrative
aml case determine --case CASE-... --actor j.dizon     # starts the five-working-day clock
aml case approve  --case CASE-... --approver r.villanueva   # a second person, always
aml due                                     # what is running out of time
aml report str --actor r.villanueva         # build and validate the file
aml submit --report-id RPT-... --actor r.villanueva    # package, digest, send or spool
aml verify                                  # prove the record has not been altered
```

### What is deliberate

- **A covered transaction and a suspicious one take different paths.** The first
  is filed because of what it is; the second only after a human determines it,
  and the five-working-day clock runs from that determination — so determination
  is a recorded event with a timestamp and an author.
- **The regulator's file layout is data, not code.** The CTR and STR field
  layouts live in versioned TOML. A schema change is an afternoon's edit under
  change control, not a release. **They must be reconciled against the AMLC's
  current published schema before the first live filing** — that is the one
  thing in this package that cannot be verified from outside your enrolment.
- **Deadlines are working days on the Philippine calendar.** A determination on
  30 March 2026 is due 8 April, nine calendar days later, because Holy Week is
  in the way. Holidays fixed by proclamation cannot be derived, so the tool
  warns for any year where none are loaded rather than computing deadlines on an
  optimistic calendar.
- **Nothing is auto-confirmed and nothing is auto-filed.** The strongest
  screening outcome is `POTENTIAL_MATCH`: confirming a sanctions match freezes a
  client's property, and a person's name goes against that decision.
- **A screening that failed is never recorded as a screening that found
  nothing.** A vendor timeout leaves the result pending, with the error attached.
- **No float ever touches an amount, and a peso equivalent is mandatory.** A
  transaction with no reference rate for its date is an ingest error: the
  threshold is in pesos, and treating a dollar figure as pesos understates it
  fifty-six-fold.
- **Alerts are identified by what they found**, so re-running monitoring after a
  rule change updates them instead of duplicating them — and never reopens one an
  analyst has already dispositioned.
- **Every state change is hash-chained.** That does not make the record
  unalterable; it makes alteration detectable, which is what matters when the
  question is whether an alert was quietly closed after the fact.

## License

Apache-2.0.
