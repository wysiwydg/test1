# Vectorized Ingestion

Step 2. Raw policy extracts into canonical staging frames, as Arrow column
operations. Builds on the [canonical data model](01-canonical-data-model.md).

---

## What this step does

```
raw extract (policy grain, one wide row per contract)
     │
     ├─ mapping.py    declarative source → canonical mapping, validated against the registry
     ├─ normalize.py  vectorized kernels: name, address, phone, email, policy number
     └─ shred.py      policy grain → three entity grains

        ├── Policy         one row per contract
        ├── Person         one row per distinct source identity
        └── Relationship   one PARTY_POLICY edge per party per policy
```

Measured on 200,000 synthetic policies (600,000 party occurrences):

| | |
|---|---|
| Throughput | **37,700 policies/s** — 5.3 s for the full shred |
| Party occurrences → distinct source identities | 600,000 → **136,647** |
| Candidate pairs vs. naive all-pairs | 21.0M vs 9.34bn — **445× reduction** |
| Largest blocking bucket | 551 rows |

---

## Everything is an expression

Every function in `normalize.py` returns a Polars **expression**, never a value.
No `map_elements`, no row iteration, no Python per record. Normalization touches
every inbound record, so a row-wise implementation would dominate the cost of
the entire pipeline.

This imposes a real constraint worth stating: the `regex` crate Polars uses has
**no backreferences and no lookaround**. Several classic string algorithms
assume both. Where that bites, the kernel is reformulated rather than dropped
back to a Python loop:

- Collapsing doubled letters is naturally `(.)\1+` → `$1`. Unavailable. Done
  instead as a 26-entry Aho-Corasick table (`AA`→`A`, `BB`→`B`, …), which is
  cheaper anyway — one pass, not a backtracking match.
- Vowel-dropping needs "not at a token boundary". `\B[AEIOUY]+` does it without
  lookaround.

## The phonetic key

`name_phonetic_key` is a **consonant-skeleton key, not Double Metaphone** — and
the docstring says so. Metaphone depends on positional rules and backtracking
that cannot be expressed as vectorized substitutions.

It collides what matters:

| | |
|---|---|
| `KATHERINE PHILLIPS` / `CATHERINE FILIPS` | → `FLPS KTRN` |
| `STEVEN CLARKE` / `STEPHEN CLARK` | → `KLRK STFN` |
| `JON KOWALSKI` / `JOHN KOVALSKI` | → `JN KFLSK` |
| `SMITH` / `SMYTH` | → `SMT` |

Two defects surfaced during testing and are fixed:

1. `JOHN` → `JHN` but `JON` → `JN`. A silent `H` survived the digraph fold.
   Now `\BH` is dropped after folding; token-initial `H` is kept, since it is
   audible and discriminating there.
2. `W`→`V` while `V`→`F` sent `KOWALSKI` to `KVLSK` and `KOVALSKI` to `KFLSK` —
   the pair that most needs to collide didn't. Both now fold to `F`, which also
   collapses `STEVEN` with `STEPHEN` (whose `PH` already folds to `F`).

Blocking keys need to be *consistent* and *high-recall*; precision is the
scorer's job, and a blocking collision costs one extra pair to reject. Trading
strict phonetic fidelity for a kernel that runs over the whole population in one
pass is the right side of that trade. A true Metaphone refiner stays available
for the undecided band, where per-record cost is affordable because the band is
small.

## The address key

Also caught in testing. The obvious key — leading number plus postcode — gets
the most common case **wrong**:

```
"Flat 2, 12 High Street"  → 2|SW1A1AA
"12 HIGH ST APT 2"        → 12|SW1A1AA     ← same door, different block
```

The key now uses the **sorted set of all numeric tokens**, so both yield
`12,2|SW1A1AA`. Different doors on one street still separate.

It's a plain string, not a digest: a steward reading a review queue can see why
two records blocked together, and it stays stable across library upgrades, which
a hash seeded by a third-party library's internals would not.

## Placeholders are nulled, not kept

`normalize_email` nulls anything that isn't an address; `normalize_phone` nulls
anything under eight digits. A blocking key built from `"N/A"` collides every
record carrying one into a single enormous bucket — the classic way a blocking
pass silently goes quadratic.

`normalize_email` deliberately does **not** fold Gmail-style dots or `+tags`.
Those rules are provider-specific and wrong for most domains: at a corporate
mail server `a.smith@` and `asmith@` are routinely two different people, and
folding them manufactures false matches exactly where a false merge does most
damage.

---

## Mapping is data, not code

Onboarding a source is a TOML file, reviewable in a pull request by someone who
doesn't read Python:

```toml
source_system = "LIFE_ADMIN"
default_country_code = "44"
date_formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y"]

[policy]
policy_number = { source = "PolicyNumber", transform = "policy_number" }
sum_assured_amount = { source = "SumAssured", transform = "money" }
currency_code = { literal = "GBP" }

[[party]]
role = "OWNER"
key_field = "OwnerCustomerId"
key_kind = "OWNER_CUSTOMER_ID"
```

Date formats are **per source**, not global. Day-first and month-first are
mutually ambiguous and no amount of inspecting values settles it reliably — the
source owner knows.

### Mappings are validated against the registry

Loading validates every canonical name against `fields.py`. This caught a real
error in my own example mapping:

```
ValueError: party[0].fields: 'phone_e164' is derived and computed by the
pipeline; it cannot be mapped from a source column.
```

`phone_e164` and `email_normalized` are declared `derived` in the registry, so a
source may not supply them — the pipeline computes them from `phone_raw` and
`email_address`. That check is what keeps step 1 and step 2 honest with each
other.

A missing inbound column is likewise reported **by name** before any work
starts, rather than surfacing later as a canonical column that is entirely null
for one day's file.

---

## Shredding, and why collapsing comes first

Party blocks are projected individually and **vertically concatenated**
(diagonally, since an agent block carries far fewer attributes than an owner
block). Work is proportional to the number of *roles* — three — not to the
number of rows.

Then `collapse_parties` does the deterministic half of entity resolution: within
one source system, the same identifier in the same namespace **is** the same
party, by definition. No scoring.

This is not an optimization detail. An agent appears on every policy they wrote,
so 200k policies carry 200k agent rows describing a few thousand agents.
Blocking before collapsing generates candidate pairs for every pair of
occurrences of the same agent — quadratic work to rediscover what the source
already stated:

| | Candidate pairs | Largest bucket |
|---|---|---|
| Blocking before collapse | 550M | 5,816 |
| Blocking after collapse | **21M** | **551** |

Policy-scoped columns (`role`, `role_sequence`, `policy_number_normalized`) are
**dropped** from the collapsed Person frame rather than carried. A party holds
different roles on different policies, so "the role" of a collapsed party isn't
a well-defined value; keeping an arbitrary one would invite downstream code to
trust it. That information lives on the edge, which is built from the
uncollapsed frame — so the two can't disagree about which parties a policy has.

Edges leave the shredder carrying **only source keys**. No `from_person_id`, no
`to_policy_id`. Resolving those here would mean guessing at identity before
matching has run; the golden writer resolves them inside the same transaction
that writes the entities.

---

## What's deliberately left for step 3

The sample data exhibits the real problem, and the numbers say exactly what
remains:

```
party occurrences (edges)         : 600,000
after deterministic collapse      : 136,647
  OWNER_CUSTOMER_ID   63,354
  INSURED_CUSTOMER_ID 63,293
  AGENT_CODE          10,000

source keys present in BOTH owner and insured namespaces: 61,156
```

Those 61,156 are the same humans held under two identifiers. Deterministic
matching **cannot** merge them — different namespaces, and the hybrid identity
assumption says an identifier is authoritative only within its own namespace and
source. Collapsing them here would be probabilistic matching performed in the
wrong place and without an audit trail.

That is precisely the work the blocking + scoring + AI-fallback stage exists to
do, and the ~136k collapsed parties are the population it runs against rather
than the 600k occurrences.

Also left for later: full registry-driven survivorship across sources
(`MOST_RECENT`, `ANY_TRUE`, source trust weights) with its `attribute_provenance`
rows. `collapse_parties` does intra-source consolidation only — most complete
occurrence wins per attribute — and does not pre-empt it.

---

## Running it

```bash
pip install -e ".[dev,vector]"
python -m scripts.generate_sample_data --rows 5000   # → data/life_admin_sample.csv
pytest                                                # 148 tests
```

The generator injects the failure modes real extracts contain, because clean
synthetic data proves nothing: the same party under different identifiers, name
order swapped, honorifics added, accents dropped, nicknames substituted, dates
and money and phone numbers in several formats per file, trusts and estates as
owners, and missing values throughout.

---

## Next step

Step 3 — entity resolution: blocking over the keys computed here, vectorized
comparator scoring driven by `match_role` in the registry, the `REVIEW`
abstention band, and the local-model fallback that only ever sees that band.
