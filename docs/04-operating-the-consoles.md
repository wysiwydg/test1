# Operating the consoles

Three audiences, three surfaces, one process. This is how you stand them up and
what each one is for.

| Console | Who | Answers |
|---|---|---|
| **Ingestion** `/console/ingest` | whoever owns a source feed | did last night's extract load, and if not, why |
| **Steward** `/console/steward` `/console/rules` | data stewards | which decisions the machine could not make |
| **Business** `/console` `/console/quality` | everyone else | who is this customer, and why does the record say that |

They are served by the same `uvicorn` process as the API, server-rendered. A
`VIEWER`'s browser never receives the date of birth it is not allowed to see,
because the masking is applied before the HTML is built rather than by the
front-end choosing what to display.

---

## 1. Start it

```bash
createdb cmdm
export CMDM_DSN="host=/tmp port=5432 user=postgres dbname=cmdm"
export CMDM_ID_HASH_KEY="$(openssl rand -base64 32)"   # never stored in the DB

python -m scripts.bootstrap
```

`bootstrap` applies the migrations and registers one principal per audience,
printing each API key **once** — secrets are stored as a hash and a
four-character prefix, so a key not written down here has to be rotated rather
than recovered.

```
  ops        INGESTOR,OPERATOR    OYoJG1bIWOjv-iOd8E0AF7IAEpGN…
  steward    STEWARD              g2ohsasBt5DoInmq9yaQHumQd8zs…
  business   VIEWER               C95phTLt7h-PQp1744ltNo-eHu_c…
```

Then two processes:

```bash
uvicorn cmdm.api.app:app --port 8000     # API, consoles, /metrics
python -m cmdm.worker serve              # drains the ingest queue
```

The worker is optional for a first look — the ingestion console has a button
that runs queued batches in the request — but in anything real it is what turns
an accepted file into golden records without anybody clicking.

Open <http://localhost:8000/console>. An unauthenticated browser is redirected
to `/console/login`; paste the key for the role you want. The key goes into an
`HttpOnly`, `SameSite=Lax` session cookie, which is what stops another site from
POSTing a merge with your credentials attached. Machine callers keep using the
`X-API-Key` header and are unaffected; the header wins when both are present.

> **What each role can reach.** `VIEWER` sees the business console only.
> `INGESTOR` sees ingestion only. `STEWARD` sees the queue and the rules.
> `ADMIN` sees everything and is the only one that reads PII unmasked by
> default alongside `OPERATOR`. The navigation only shows what your roles
> permit, and the pages refuse directly as well — a hidden link is not access
> control.

---

## 2. Ingestion

**`/console/ingest`**

Pick a mapping, choose a CSV, submit. Three things happen, and the split
between them is the design:

1. **Validation is synchronous.** You are told immediately which columns are
   missing, which dates do not parse under the configured formats, and which
   party blocks are empty — with example values, because *"column OwnerDOB has
   412 unparseable values"* is a report and *"e.g. 31/02/2019, N/A,
   0000-00-00"* is a diagnosis. A file with errors is recorded as `REJECTED`
   and lands nothing.
2. **Landing and queueing are one commit.** The raw rows reach `source_record`
   and the job reaches `work_queue` in the same transaction. With a separate
   broker those are two commits and the window between them is where a batch
   becomes accepted-but-lost.
3. **Processing is not synchronous.** The batch sits in the queue until a
   worker takes it.

The page shows the queue depth and the last 25 batches with their state and
their job's state, so "is it stuck, did it fail, or has nobody run it" is one
glance rather than three.

**States you will see.** `VALIDATED` → `LANDED` → `COMPLETED` is the happy path.
`REJECTED` means validation refused it; the reason is in the detail column.
`FAILED` means a worker tried and could not, with the error on the batch as well
as on the job — an operator should not have to know the queue exists to find out
what happened to their file.

**Re-uploading the same file is safe.** A byte-identical redelivery from the
same source is detected by content hash and ignored, and you are told so rather
than being thanked for a batch that did nothing. Feeds re-send far more often
than anyone expects.

**Re-processing is also safe**, and this is worth stating plainly because the
button invites it: running a batch twice writes nothing the first run did not.
Golden ids come from the crosswalk rather than being minted per run, the writer
is SCD-2 against a record hash that excludes audit columns, and survivorship
breaks every tie deterministically. On the reference batch, runs two and three
report `changed=0, unchanged=2711`.

### Measured, 5,000-policy extract

| | |
|---|---|
| Validate, land, enqueue | 1.3 s |
| Process (shred → standardize → resolve → survive → write) | 3.2 s |
| 5,000 policies → 3,881 source identities → **2,711 golden persons** | |
| Deterministic standardization pass | 86.8% — the rest went to a local model |
| Candidate pairs after blocking | 4,847, a 1,553× reduction |

---

## 3. Stewardship

Two queues, and they are different kinds of work.

### `/console/steward` — pairs

Everything the scorer put in the grey zone, most-likely-duplicate first,
scoped to the latest resolution run. A steward's attention is the scarcest
resource in the system and an unordered queue spends it randomly.

Each row shows the composite score, the model's advisory verdict, both sides
with their source identity, and **what the last run actually did** — merged, or
kept apart. That last column matters: a pair the model already merged is a
*risky merge* to check, and a pair it rejected is a *missed merge*. Both are in
the queue for the same reason, and they need opposite scrutiny.

**Merge** and **Separate** both require a reason. The decision is recorded, not
applied: it is written to the ledger as `decided_by = 'STEWARD'`, and the *next*
run reads it as a fixed edge. So —

- a steward's call survives re-resolution instead of being overwritten by it,
- the customer book does not change under whoever is reading it right now,
- and the review is spent once rather than every night.

Decided pairs move to **Decided by a steward** below the queue. To apply them,
re-process the batch from the ingestion console (or wait for the next run).

> **One limit, stated rather than hidden.** A `SEPARATE` verdict can still be
> defeated by transitivity: if the scorer independently merged A–C and C–B,
> connected components will put A and B together regardless. That is inherent to
> clustering by transitive closure. The queue surfaces the result as a
> suspicious cluster rather than pretending the override held.

### `/console/rules` — learned rules

The human gate in the standardization loop. When the local model handles the
same *shape* of edge case repeatedly, the miner proposes a deterministic regex
that would have handled it, shadow-evaluates it, and parks it here.

Run the miner after a batch has been processed:

```bash
python -m cmdm.worker mine
```

It reads the whole exception log, so it is deliberately not on the ingest path —
running it per batch would repeat the same global scan and re-propose the same
rules. On the reference batch it proposes six rules from 512 logged exceptions.

Each row shows the pattern, how many records it **fixes**, how many it
**regresses**, and the evidence that motivated it. The regression count is the
one that matters. Measuring only what a rule fixes will approve a rule that
fixes a thousand records and quietly breaks ten thousand, because nobody looked
at the ten thousand.

Approve, and the rule becomes `ACTIVE` and runs in the deterministic pass from
then on — so those records never reach a model again. That is the loop: the AI
share of a batch is a number that should fall over time, and it is measured on
every run rather than assumed.

---

## 4. Business view

**`/console`** — search by name or email. The search normalizes your query the
same way ingestion normalized the data, so "Bob O'Brien" finds the record stored
as `OBRIEN ROBERT`.

Personal fields are masked unless your role carries `UNMASK`. A `VIEWER` is told
so on the page rather than being shown blanks and left to wonder.

**`/console/person/{id}`** — one golden record, and on the same page:

- **Household and connections** — who this party lives with as a family, how
  each of them is related, and on how many policies that rests. Below it,
  **Belongs to**: the companies, trusts and estates the party is linked to.

  The two are separate on purpose. A household is a family; an affiliation is a
  legal entity. A company insuring forty staff is not a household of forty-one,
  and merging the two is the standard way this feature produces nonsense.

  Households are built from what the source *stated* — insurable interest is a
  condition of issue, so a life admin system records at application that the
  owner is the insured's spouse, parent or child. They are **not** built from
  shared addresses. Two people at one postcode are two people: the sample
  extract contains flatmates who share an address with a family and belong to
  no household, and spouses who kept their own surname and belong to one.
  Address is used only to corroborate, never to admit a member.

  Membership is transitive over stated relations, so a member two steps away is
  in the household without any edge naming the relation directly. Those members
  are listed as "same household" rather than with a guessed relationship.

- **Contributing sources** — every source key that resolves to this person.
- **Why these values** — per contested attribute: the surviving value, the rule
  that selected it, the source that won and how many candidates there were.
  Only *contested* attributes appear; everything else was uncontested and saying
  so for every column would bury the decisions that need review.
- **Version history** — SCD-2. Golden records are never updated in place.

An id retired by a merge still resolves; the page says which record it followed
to. Ids are published to downstream systems and cannot be invalidated by an
internal merge.

**`/console/entities`** — what is actually in the three canonical entities.
Counts first (policies, golden persons, relationships, landed rows), then the
breakdowns that say what kind of book this is: roles on the edge, policy status,
party type. Below that a browser over any of the three, fifty rows a page.

The three entities are not three views of the same thing. **Policy** is the
grain the data arrives in. **Person** is what resolution collapses it to.
**Relationship** is the role-bearing edge: 15,000 sourced ones in the sample,
one per party per policy, and the reason `role` is an attribute of an edge
rather than three columns on Policy — plus the derived party-to-party edges the
householding pass adds on top.

The **Household** section counts what the sources say about who lives with whom:
households, how many parties are in one, the largest, and how many parties are
linked to a company, trust or estate. Measured on the 5,000-policy sample
against the generator's own ground truth: 598 households derived, every one of
them a single real family, no flatmate wrongly included, and 80% of the real
families of two or more found intact. The 20% not found are families whose
members never appear together on a policy with a stated relationship — no
evidence, no household, which is the right answer rather than a guess.

The browser masks personal fields for a role without `UNMASK`, exactly as search
and export do. A page that showed what the other two withhold would be the way
around masking rather than a view of it.

**`/console/export`** — two different files, for two different questions.

*The hand-back file* (`/console/export/source/{mapping}.csv`) is the one that
matters to a source system. One row per row delivered, the columns the mapping
reads, in the grain the file arrived in — plus `PolicyMdmId`, `OwnerMdmId`,
`InsuredMdmId` and `AgentMdmId`. It joins to what the source already holds
because it *is* what the source already holds, with four columns added. A dump
of golden records would be correct and unusable: the source has no key to join
it on.

Two value modes. **As delivered** keeps every value exactly as it arrived, so
the file is recognisable as the one that was sent and the ids can be loaded
without adopting anything else. **Golden values** replaces the party attributes
with the ones that survived resolution, for a system taking the cleaned data
too.

A row that has landed but not yet resolved is exported with empty id columns
rather than dropped. A hand-back file that silently loses rows cannot be
reconciled against what was sent, which is the first thing anyone loading it
will try to do.

*The entity files* (`/console/export/entity/{entity}.csv`) are the golden
records themselves — one file per entity, every column the registry declares.
For analysis, not for handing back.

Both stream over a server-side cursor, so a book of any size exports in constant
memory, and both are masked to the caller's role and written to the access log
with the row count and whether PII was revealed.

**`/console/quality`** — completeness and conformity per attribute, plus how
many records are held under two or more source keys. Deliberately *not* the
operational dashboard: queue depth and dead letters belong to whoever runs the
pipeline, and showing both together teaches each audience to ignore half the
page. Operational metrics are on `/metrics` in Prometheus exposition.

---

## 5. A full loop, end to end

```bash
# 1. an operator submits a file
open http://localhost:8000/console/ingest       # sign in as `ops`, upload

# 2. a worker picks it up   (or press "Process queued batches now")
python -m cmdm.worker once

# 3. a steward reviews what the machine would not decide
open http://localhost:8000/console/steward      # sign in as `steward`

# 4. the miner proposes rules from what the model had to handle
python -m cmdm.worker mine
open http://localhost:8000/console/rules        # approve, with reasons

# 5. re-process: steward decisions become fixed edges, approved rules run
#    in the deterministic pass, and the AI share falls
open http://localhost:8000/console/ingest       # "Process queued batches now"

# 6. the business sees the result
open http://localhost:8000/console              # sign in as `business`
open http://localhost:8000/console/entities     # what is in all three entities

# 7. the source system gets its extract back, with MDM ids attached
open http://localhost:8000/console/export
```

---

## 6. Troubleshooting

**The steward queue says "Nothing in the grey zone."**
Either no batch has been processed, or every pair was decided confidently. Check
`/console/ingest` for a batch in `COMPLETED`, and `select count(*) from
mdm.match_pair` for a non-zero row count.

**A batch sits at `LANDED` forever.**
Nothing is draining the queue. Start `python -m cmdm.worker serve`, or press the
process button.

**A batch is `FAILED`.**
The error is on the batch row and on the job. The job retries with exponential
backoff up to `max_attempts` and then dead-letters; the ingestion console shows
the dead-letter count.

**The console redirects to sign-in in a loop.**
The key was not accepted. Keys are shown once by `scripts.bootstrap`; register a
new principal rather than guessing:

```bash
python -m scripts.bootstrap --skip-migrations --principal "alice:STEWARD"
```

**Uploading the same file does nothing.**
That is the redelivery guard. The page says so. Change the file or accept that
it is already in.

**`/console/rules` is empty.**
The miner has not run. It is offline by design: `python -m cmdm.worker mine`.
