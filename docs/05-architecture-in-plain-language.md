# How the customer master works

*A plain-language companion to [`03-architecture.md`](03-architecture.md),
which covers the same system for engineers.*

Your policy systems know your customers under many different names and numbers.
This system works out which of them are the same person. It uses AI to do that
— but only for the small share of cases where nothing simpler will do, and it
writes down every time it does.

---

## The problem: the same person, five different ways

A policy record arrives with an owner, a life insured and an agent packed into
one row. The same customer turns up again next month as *Mrs K. O'Brien*
instead of *Katherine OBrien*, at the same address with the postcode spaced
differently, and with a date of birth written the American way round.

A human can see those are one person in about a second. A computer comparing
text cannot, and there are too many records for humans to check. So the
question this system answers is: **which of these records are the same
customer, and what should that customer's details actually say?**

| | |
|---|---|
| Policies in | 5,000 |
| Real customers out | 2,389 |
| Households found | 597 |
| Time to do all of it | about 5 seconds |

---

## The design: cheap work first, AI only for what is left

Most of the messiness in customer data is boring and repetitive: extra spaces,
inconsistent capitals, dates in three formats, a postcode with the space in the
wrong place. None of that needs intelligence. It needs rules, applied to
millions of rows at once, which is very fast and very cheap.

So the system does all of that first, and then asks a simple question about
each record: *is this good enough to use?* Records that pass never go near a
model. Only the ones that fail do.

```
  Out of every 100 customer records

  ┌──────────────────────────────────────────────┬──────────────┬───────┐
  │ 86  cleaned by plain rules — no AI involved  │ 13 → model   │ 1 flag│
  └──────────────────────────────────────────────┴──────────────┴───────┘

  Out of every 100 "could these two be the same person?" comparisons

  ┌────────────────────────────────────────────────────────────────┬─┐
  │ 99  obvious either way — decided without AI                    │1│
  └────────────────────────────────────────────────────────────────┴─┘
```

**Why this matters commercially.** An AI that reads every record costs in
proportion to *how much data you have*. An AI that only sees what the simple
rules could not handle costs in proportion to *how messy your data is*. Those
are very different bills — and only the second one goes down when your source
systems improve.

---

## The AI, specifically: two jobs, both of them narrow

The system uses AI in exactly two places. Not for reporting, not for chat, not
for writing records. Two decisions, both of which a person would find easy and
a rule finds impossible.

**Reading a name apart.** Given *de Souza, Maria Anne* or *DR PETER
MURPHY-O'BRIEN*, which part is the first name and which is the family name?
There is no rule that gets this right across cultures. A model that has seen a
lot of names does much better. This is 13% of records.

**Judging a borderline match.** Are *Bob Fletcher* and *Robert Fletcher* at the
same address the same person? A text comparison scores those two names as
barely similar. A model that knows Bob is short for Robert does not. This is
about 1% of comparisons — the ones the system has already decided it cannot
call either way.

> **The AI is the exception handler, not the engine.**

Both models run **on your own machine**. Nothing is sent to any AI provider,
there is no per-use billing, and no customer data leaves the building. That
matters most precisely here, because the records that reach a model are the
ones with the messiest personal details in them.

---

## The guard rails: what the AI is not allowed to do

The value of an AI decision is not just whether it is right. It is whether you
can find out afterwards what it decided and why. So the model's authority is
deliberately limited, and the limits are built into the system rather than
written in a policy document.

- **It cannot create or merge a customer on its own.** It offers an opinion on
  one comparison. Deciding what becomes a customer record is done by the
  system, from all the evidence together.
- **It cannot overrule a hard contradiction.** Two different dates of birth, or
  a person against a company, refuses the match no matter how confident the
  model is.
- **It cannot put its own rules into production.** When it spots a repeating
  pattern it proposes a fix — and a human approves it, with a reason, before it
  ever runs.
- **It cannot change a customer's details directly.** It suggests a value. That
  value competes with what every other system said, under rules you set.
- **It cannot decide quietly.** Every single decision is stored with the
  model's name and version. Months later you can ask "why are these two the
  same person" and get an answer.

**Kept on the record.** On the reference file the system stores 11,931 match
decisions — including the 11,834 it decided *against*. Keeping the rejections
is deliberate: "why were these two not merged?" gets asked as often as the
opposite, and it is unanswerable later if you only kept the merges.

---

## Getting cheaper: it hands work back to the rules

Every time the model is called, the system records what it was asked and why
the simple rules failed. It then looks for patterns. When the same shape of
problem has come up at least 25 times, it proposes a plain rule that would have
handled it.

```
  same problem  ─>  a rule is  ─>  tested on  ─>  a person
  25+ times         proposed       past data      approves it
       ↑                                              │
       └──────────────────────────────────────────────┘
        from now on the fast rules handle it —
        the model never sees it again
```

A proposed rule is run against past data twice: once to check it fixes what it
claims to, and once to check it breaks nothing that already worked. Both
results are shown to the person approving it.

The approval step is not a formality. A cleaning rule that promotes itself
unreviewed can corrupt every record it touches *in the same way* — and that is
the hardest kind of damage to spot, because nothing looks unusual next to
anything else. So no rule goes live without someone saying yes.

---

## Households: where we chose not to use AI

The system can tell you which customers live together as a family, and which
belong to a company, a trust or an estate. This is the place where reaching for
a model would be the obvious move — and we deliberately did not.

Insurance systems already record *why* someone is allowed to own a policy on
another person's life: spouse, parent, child, employer. That is a fact the
source stated. So households are built from those statements, not guessed from
who shares a postcode.

**What that buys.** Of 597 households found, every one is a single real family
— no accidental merging of two families, and no flatmates swept in just because
they share an address. Spouses who kept their own surname are still found,
because the source said "spouse".

The cost is honest too: about 20% of real families are not found, because their
members never appear together on a policy with a stated relationship. No
evidence, no household — which is a better answer than a confident guess.

A company is never treated as part of a household. It insures its directors; it
does not live with them. A firm insuring forty staff is not a household of
forty-one, and keeping the two apart is most of what stops this kind of feature
producing nonsense.

---

## Four things worth remembering

1. **AI is the exception path.** 13% of records and 1% of comparisons. The
   other 99% is decided by rules that cost almost nothing.
2. **It runs on your hardware.** No cloud AI service, no usage bill, no
   customer data leaving the building.
3. **Nothing it decides is invisible.** Every decision is stored with the model
   that made it, including the decisions to say no.
4. **Its share falls over time.** Recurring problems get turned into reviewed
   rules, and those cases stop reaching the model at all.

*All figures measured on the 5,000-policy reference extract, not estimated.*
