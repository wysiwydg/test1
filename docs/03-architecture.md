# Architecture

Where the data lives, and where the models are allowed to touch it.

A plain-language companion to this document is
[`05-architecture-in-plain-language.md`](05-architecture-in-plain-language.md).
It covers the same system for a business audience.

Every number here is measured on the 5,000-policy reference extract in
`data/life_admin_sample.csv`, not estimated. Reproduce them with
`./verify.sh` or `python -m cmdm.worker backfill`.

The architectural claim is not that AI is used. It is that **AI is a branch off
the path rather than a stage on it** — and that every branch it takes is written
down.

| | |
|---|---|
| Canonical entities | 3 |
| Tables in the store | 21 |
| Records seen by a model | **13.0%** |
| Candidate pairs seen by a model | **0.81%** |
| Model decisions logged | 100% |

---

## 1. The store

Source data arrives at policy grain: one wide row per contract, carrying an
owner, an insured and an agent side by side. The store holds three entities
instead, because a party is not a property of a contract and a role is not a
property of a party.

| Entity | Grain | Fields | Identity |
|---|---|---|---|
| **Policy** | One contract | 45 | Deterministic — policy number within a source system |
| **Person** | One party, natural or legal | 57 | No natural key; identity lives in a crosswalk |
| **Relationship** | One edge | 30 | The assertion a source made, or a derivation from several |

Person deliberately has *no* unique index on any business column. Two people
genuinely share a name and a date of birth; a database that forbids it is a
database that will reject the truth. Identity is therefore a stored mapping —
the crosswalk — rather than a constraint, and that mapping is what resolution
writes.

```
  source_record ──shred──> policy ────FK──> policy_master ──> policy_xref
  (immutable                person ────FK──> person_master ──> person_xref
   landing zone)            relationship                       (system·kind·key
        │                                                       → person_id)
        │
        └──────────────> attribute_provenance
                         which source won each attribute, by which rule,
                         traced back to the exact landed record
```

Foreign keys point at the anchor tables (`*_master`) rather than the versioned
tables: a surrogate key repeats once per version, so the partial unique index
over current rows is not a legal foreign-key target. Relationship has no anchor
because nothing references an edge.

Around those seven tables sit fourteen more: the work queue, the resolution
ledger, the standardization exception log and rule store, consent and erasure
records, the principal table and an append-only access log. Twenty-one in
total, all generated from one field registry in `model/fields.py`, which also
projects the Arrow schemas and the API contracts. A drift test regenerates the
DDL and fails the build if the committed schema no longer matches.

---

## 2. Runtime path

A batch is validated and landed synchronously; everything after that is a
queued job.

```
 land ─> shred ─> standardize ─> resolve ─> survive ─> write ─> household
                       │             │
                  13.0% of      0.81% of
                   records        pairs
                       ↓             ↓
              local standardizer   grey-zone classifier
              ONNX int8 · CPU      cross-encoder
                       │             │
                  returns,      returns, stamped ──> steward queue
                  stamped                            (a person decides;
                                                      next run applies it)
```

Both AI detours **leave a stage and return to the same stage**. Six of the seven
stages never reach a model at all. The two that can are the ones where the
cheap, vectorized answer is structurally unavailable: separating a given name
from a surname in a string nobody agreed the format of, and judging whether
`Bob` and `Robert` are the same person.

**The economic argument.** A model that sees every record costs in proportion
to how much data there is. A model that sees only what the deterministic pass
could not handle costs in proportion to how *messy* the data is. Those are
different quantities, and only the second falls when the feeds improve.

---

## 3. AI, per record: the quality gate

After the vectorized pass, twelve declared checks run as a single Polars
expression over the batch — one pass, regardless of how many checks exist. Each
names a field, a predicate and a reason. A record that passes every check never
touches a model.

The checks answer *"is this usable"*, not *"is this correct"*. The gate cannot
know whether `JOHN SMITH` is the right name. It can know that a name which
produced no phonetic key cannot be blocked on, and that a party the matcher
cannot block on is a party the matcher will silently never compare.

```
  2,407 party records
        │
      [gate · 12 checks]
        ├─ passes every check ──────> 2,077   86.3% · no model, ever ─┐
        ├─ name unparseable ────────>   314   13.0% · to the model ───┤
        └─ date of birth implausible >    21   logged, not invented    │
                                                                      ↓
                                                    2,385 usable · 99.1%
```

The 21 records the model cannot help are not silently dropped. A date of birth
outside a plausible range is a data problem, and inventing one would be worse
than recording that it is missing.

**What the standardizer is.** Two implementations behind one protocol.
`OnnxStandardizer` runs a small sequence-labelling model quantized to int8 on
CPU — no network call, no per-token billing, no data leaving the host, which
matters because these are precisely the records carrying the messiest personal
data. `HeuristicStandardizer` is a real second implementation, not a stub: it
resolves initials, particled surnames, comma-inverted order and run-together
addresses with branchy logic that does not vectorize. The numbers above were
produced by the heuristic path, which is the accuracy floor a real model has to
beat.

Inference is per-record and deliberately not optimized. If one shape of input
becomes common enough for that to hurt, that is the signal the mining agent
watches for, and the answer is a deterministic rule rather than a faster model.

---

## 4. AI, per pair: the grey zone

Resolution never compares all pairs. Blocking generates candidates from keys
computed at ingest — sorted name tokens, a phonetic key, an address key — which
turns 2.9 million possible pairs into 11,931 candidates, a reduction of 243:1.
Each candidate is scored by vectorized comparators and lands in one of three
zones.

```
  score  0.00 ─────────────── 0.50 ────── 0.85 ────── 1.00
         │  11,834 auto-reject  │  97 grey │  0 auto-match
         │  no model            │  0.81%   │
                                     ↓
                            cross-encoder, accepts at ≥ 0.70
                                     ├──> 18 merged
                                     └──> 79 held apart

  1,796 pairs vetoed — conflicting DOB, or person vs legal entity.
  A veto outranks any score, including the model's.
```

| Threshold | Value | Why there |
|---|---|---|
| Auto-match | ≥ 0.85 | Merged without review. High, because an unmerged duplicate is visible and a wrong merge is not. |
| Auto-reject | < 0.50 | Recorded and dropped. The rejections are kept — "why did these *not* merge" is asked as often as the reverse. |
| Model accepts | ≥ 0.70 | Above 0.5 deliberately. A grey-zone pair is one the deterministic evidence could not support, so the model must be confident rather than merely inclined. |
| Mining evidence | ≥ 25 | Occurrences of one pattern before a rule is even proposed. |

Auto-match is zero on this extract because the mapping declares one
customer-number space: parties sharing a customer number are collapsed
deterministically before scoring runs, so nothing is left for probability to
rediscover.

Clustering is transitive: accepted pairs form a graph and connected components
become master records. That transitivity is why the input is restricted to
pairs the pipeline actually accepted — one wrong edge silently unions two
clusters, and the damage is proportional to the size of both. Components above
a size threshold are flagged rather than trusted.

---

## 5. The learning loop

Everything the fallback handles is logged with the check it failed and a
structural signature of the input. The mining agent groups those exceptions,
and a signature recurring at least 25 times becomes a candidate rule — *data,
never code*. The worst a bad rule can do is rewrite a string badly.

```
  exceptions ─> PROPOSED ─> SHADOW ─> APPROVED ─> ACTIVE
  (what the     a mined     tested on  by a       in the
   model saw)   regex       past data  steward    fast pass
       ↑                       │                     │
       └───────────────────────┼─────────────────────┘
                               │      next run: these records
                               │      never reach a model again
                    the database refuses an ACTIVE rule that was
                    never shadow-tested or that regressed anything
```

Shadow evaluation is the step that matters, and it is why auto-promotion was
rejected. Normalization that rewrites itself into production unreviewed can
corrupt every record it touches *uniformly* — and uniform corruption is the
hardest kind to notice, because nothing looks anomalous relative to anything
else.

The agent is a pattern miner rather than a language model writing regexes
freehand. A generated regex that is *nearly* right is more dangerous than one
that is obviously wrong: it passes review and then quietly mangles a shape
nobody anticipated. Mined patterns are narrow by construction, each anchored to
the exact token shape it came from.

---

## 6. What the models are never allowed to do

| Boundary | Enforced by |
|---|---|
| Never mints, merges or retires an identity | Clustering runs on accepted pairs; the crosswalk is written by the pipeline, not by a model |
| Never overrides a veto | Conflicting dates of birth, or a natural person against a legal entity, refuse the pair whatever it scored |
| Never promotes its own rule | A steward approves, with a reason, and the database refuses an untested ACTIVE rule |
| Never writes a golden value directly | Survivorship is registry-driven; the model supplies a candidate value like any other source |
| Never leaves the host | ONNX Runtime on CPU. No network call, no per-token billing, no data egress |
| Never decides without saying so | Engine name and version are written onto every decision |

The last one is what makes the rest checkable. Every grey-zone verdict lands in
`match_pair` with its score, its zone and the engine that produced it; every
fallback invocation lands in `standardization_exception` with the checks it
failed. On the reference extract that is 11,931 pair decisions and 314
exceptions retained — including the 11,834 rejections, because "why were these
two *not* merged" is asked as often as the opposite and is unanswerable after
the fact if only the merges were kept.

**Reproducibility.** Re-processing the same batch writes nothing: 22,389 rows
unchanged, zero changed. The golden writer compares a content hash over
business fields only, so audit columns moving does not manufacture a version.
Both models write their name and version onto their decisions, so a run can be
explained months later without reference to deployment configuration nobody
recorded.

---

## 7. Which model is allowed to run

Sections 5 and 6 hold a standardization *rule* to a high standard: mined from
evidence, shadow-tested against past data, approved by a named steward with a
reason, and refused by the database if any of that is missing. Until now the
*models* those rules exist to replace were held to none of it. Which model ran
was an environment variable pointing at a file. Swapping it was a deployment
action with no measurement, no approval and no trace — the exact shape of change
this system refuses everywhere else.

A model version now moves through the states a rule moves through:

```
  CANDIDATE ──evaluate──> SHADOW ──promote──> ACTIVE
   registered,             measured,          the model
   fingerprinted           not yet trusted    its kind runs
                                                   │
                                                RETIRED
                                              (kept, never deleted —
                                               its past decisions
                                               still name it)

        the database refuses an ACTIVE model with no recorded
        evaluation and no named approver
```

Two properties make the record load-bearing rather than decorative.

**Evidence is measured, not asserted.** Promotion requires metrics produced by
running the candidate over a benchmark whose answer key is known — the harness
in section 8. A model registered with numbers somebody typed in is a model
nobody measured, and `promote` refuses it.

**The artifact is fingerprinted.** A file on disk can change after it was
evaluated, and then the model running is not the model that was approved. The
SHA-256 recorded at registration is what makes that detectable instead of
assumed: an ACTIVE model whose artifact no longer matches is refused at load
time rather than quietly run.

Precedence when selecting an engine is explicit, and the registry outranks the
environment: an approved model beats `CMDM_STANDARDIZER_MODEL`, because an
environment variable is a deployment detail nobody reviewed.

| | |
|---|---|
| Explicit path passed in code | tests and one-off experiments |
| **ACTIVE model in the registry** | **what production runs** |
| `CMDM_*_MODEL` environment variable | legacy escape hatch |
| Reference implementation | the default, and fully functional |

With nothing promoted, both AI paths run their reference implementations — the
heuristic standardizer and the feature cross-encoder. That is not a degraded
mode; it is what every figure in this document was measured on. A model is an
optimization the system is built to survive the absence of.

---

## 8. Measuring whether matching works

Thresholds are the most consequential numbers here and the easiest to change
casually. Lowering auto-match merges more parties, which improves every figure
the consoles show — more resolved, fewer duplicates, a tidier book — right up
until somebody's policy is attached to a stranger. No other check in this system
catches that, because "did resolution do the right thing" has no answer without
knowing what the right thing was.

So the harness builds an extract whose answer key is known: a share of parties
arrive under two customer numbers, the second registration missing a date of
birth or carrying a different email, the way a real re-registration does. The
generator knows which pairs those are; nothing is read back out of the thing
being measured.

```
worker evaluate --rows 2000 --duplicate-rate 0.18
```

| Measure | What a regression in it means |
|---|---|
| Blocking recall | a pair blocking never generated is invisible to every stage after it — no comparator, no model and no steward will ever see it. This is the ceiling on recall |
| Precision | a wrong merge silently destroys two records; an unmerged duplicate is visible and fixable. Held tighter than recall for that reason |
| Recall | duplicates left in the book |
| Largest wrong cluster | merging is transitive, so one bad edge joins two clusters entirely — damage a pair metric cannot see, because the bad pair is one row while the cost is proportional to both clusters |
| Model true vs false positives | whether the grey zone is buying recall or just buying merges |

Measured at 2,000 rows: blocking recall 0.934, precision 0.936, recall 0.721,
F1 0.815. The floors asserted in the suite sit deliberately below those, because
a floor set at the current value fails on noise and gets raised until it means
nothing.

**What it found immediately.** Most false positives share one shape: an agent
record auto-matching a customer record on name alone at ~0.975. Agent blocks
carry a name, an email and a phone — no date of birth, no address — so the
comparators that would disagree have nothing to compare, and a high score over
three fields is treated exactly like a high score over eight. *The score does
not account for how much evidence it rests on.* That is a real gap in the
scoring model, surfaced by measurement rather than by inspection, and it is
recorded here rather than quietly patched.

---

## 9. Households: derived without a model, on purpose

Householding is the place where a model would be the obvious reach, and it is
deliberately not used. Insurable interest is a condition of issue, so a life
administration system records at application that the owner is the insured's
spouse, parent or child. That statement is evidence. A shared postcode is not.

Households are therefore the connected components over *stated* family
relations only, with address used to corroborate and never to admit a member.
Measured against the reference extract's own ground truth: 597 households
derived, every one of them a single real family, no flatmate wrongly included,
and 81.4% of real families of two or more found intact. The 19% not found are
families whose members never appear together on a policy with a stated
relationship — no evidence, no household, which is the right answer rather than
a guess.

Affiliations to companies, trusts and estates are counted separately
throughout, because a company insuring forty staff is not a household of
forty-one.

---

## 10. Process topology

| Process | What it does | Scaling |
|---|---|---|
| API / console (`uvicorn`) | Validates and lands batches, serves reads, renders the consoles | Stateless; scale horizontally |
| Worker (`python -m cmdm.worker serve`) | Claims jobs with `FOR UPDATE SKIP LOCKED`, runs the pipeline | Add processes; the queue arbitrates |
| PostgreSQL | The golden store, the landing zone, the queue and the audit trail | The single stateful component |

Each job is its own transaction and its own connection. A worker that shared
one transaction across jobs would turn any single bad batch into a rollback of
every batch it had already finished, which is precisely the failure the queue
exists to contain.

---

## What re-running has to preserve

1. **Golden ids are stable.** They come from the crosswalk, never minted fresh
   for an identity already known.
2. **An unchanged record is not a new version.** Change detection is a content
   hash over business fields only.
3. **Rejections survive.** The match ledger keeps what it refused, not only
   what it merged.
4. **A retired id still resolves.** Merge pointers keep published ids working.
5. **The landing zone is never rewritten.** It is what every later release
   recomputes from.
