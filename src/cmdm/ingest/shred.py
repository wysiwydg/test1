"""Shredding: one wide policy row into three canonical frames.

The source arrives at policy grain — a row carrying the contract terms plus the
party details for every role side by side. The canonical model is at three
different grains. This module performs that split:

    one raw row  ->  1 Policy
                 ->  N Person   (one per mapped party block)
                 ->  N Relationship  (one PARTY_POLICY edge per party)

The whole shred is expressed as lazy Polars operations. Party blocks are
projected individually and then **vertically concatenated**, which is what keeps
this vectorized: each block is a column projection over the full batch, so the
work is proportional to the number of *roles* (three) rather than to the number
of rows (millions). A row-wise loop that emitted parties per record would be the
obvious implementation and would also be the one that fails at scale.

Derived columns are computed here, once, in the same pass. Nothing downstream
recomputes a blocking key.
"""

from __future__ import annotations

import polars as pl

from cmdm.ingest import normalize as N
from cmdm.ingest.mapping import FieldMapping, PartyMapping, SourceMapping, Transform
from cmdm.model.enums import EdgeKind, NameParseMethod
from cmdm.model.fields import MONEY_SCALE

__all__ = [
    "shred_policies",
    "shred_parties",
    "collapse_parties",
    "shred_relationships",
    "shred",
    "POLICY_SCOPED_COLUMNS",
    "PASSTHROUGH_COLUMNS",
]

#: Columns carried straight through from the raw frame when present, without
#: being declared in a mapping. Only ``source_record_id``, and it is here
#: because survivorship needs to name the record a winning value came from --
#: both for the lineage the console shows and for the deterministic tie-break.
#: Without it a tie falls back to row order, and Polars group-by is threaded, so
#: two runs over identical input can disagree about which of two equally-good
#: values wins. A customer's name changing overnight for no reason is exactly
#: the kind of unexplainable churn an MDM system exists to prevent.
PASSTHROUGH_COLUMNS = ("source_record_id",)

#: Columns that describe a party's attachment to one policy rather than the
#: party itself. They belong on the Relationship edge; carrying them on a
#: collapsed Person row would assert something untrue, since a party holds
#: different roles on different policies.
POLICY_SCOPED_COLUMNS = (
    "role", "role_sequence", "policy_number_normalized", "stated_relationship",
)


def _passthrough(raw: pl.LazyFrame) -> list[pl.Expr]:
    """Reserved columns to carry through, if the caller supplied them.

    Optional rather than required: the shredder is used directly on a CSV in
    tests and in the README, where there is no landing zone and therefore no
    record id to carry.
    """
    available = set(raw.collect_schema().names())
    return [pl.col(c) for c in PASSTHROUGH_COLUMNS if c in available]


def _apply_transform(fm: FieldMapping, mapping: SourceMapping) -> pl.Expr:
    """Build the expression producing one canonical column from one source column."""
    if fm.literal is not None:
        return pl.lit(fm.literal).alias(fm.canonical)

    col = pl.col(fm.source).cast(pl.String, strict=False)

    match fm.transform:
        case Transform.TEXT:
            expr = col.str.strip_chars()
        case Transform.NAME:
            # The raw name is preserved exactly; the normalized and derived
            # forms are added alongside by _party_frame.
            expr = col.str.strip_chars()
        case Transform.EMAIL:
            expr = N.normalize_email(col)
        case Transform.PHONE:
            expr = N.normalize_phone(col, mapping.default_country_code)
        case Transform.POLICY_NUMBER:
            expr = N.normalize_policy_number(col)
        case Transform.DATE:
            expr = N.parse_date(col, tuple(mapping.date_formats))
        case Transform.MONEY:
            # Strip currency symbols and thousands separators before casting.
            # Decimal, never float: these amounts get summed and reconciled.
            expr = (
                col.str.replace_all(r"[^0-9.\-]", "")
                .cast(pl.Float64, strict=False)
                .round(MONEY_SCALE)
                .cast(pl.Decimal(38, MONEY_SCALE), strict=False)
            )
        case Transform.INTEGER:
            expr = col.str.replace_all(r"[^0-9\-]", "").cast(pl.Int64, strict=False)
        case Transform.BOOLEAN:
            from cmdm.ingest.mapping import TRUE_TOKENS

            expr = col.str.strip_chars().str.to_uppercase().is_in(list(TRUE_TOKENS))
        case Transform.ENUM:
            expr = N.normalize_enum(col, dict(fm.vocabulary))
        case _:  # pragma: no cover - guarded by FieldMapping.__post_init__
            raise ValueError(f"unhandled transform {fm.transform!r}")

    return expr.alias(fm.canonical)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def shred_policies(raw: pl.LazyFrame, mapping: SourceMapping) -> pl.LazyFrame:
    """Project the policy-grain canonical columns from a raw batch.

    Deduplicated on the normalized policy number. A single file legitimately
    carrying one policy twice — a full extract concatenated with a delta is the
    usual cause — must not produce two policy rows, and catching it here is
    cheaper than resolving it after both have reached the golden store.
    """
    exprs = [_apply_transform(fm, mapping) for fm in mapping.policy]
    frame = raw.select(
        *exprs,
        *_passthrough(raw),
        pl.lit(mapping.source_system).alias("source_system"),
    )

    frame = frame.with_columns(
        N.normalize_policy_number(pl.col("policy_number")).alias("policy_number_normalized")
    )
    return frame.unique(subset=["source_system", "policy_number_normalized"], keep="first")


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------


def _party_frame(
    raw: pl.LazyFrame, party: PartyMapping, mapping: SourceMapping
) -> pl.LazyFrame:
    """Project one party block into Person-shaped rows.

    Every derived name column is computed here in the same pass as the
    projection, so a batch leaves the shredder already blockable.
    """
    exprs = [_apply_transform(fm, mapping) for fm in party.fields]

    # What the source said about this party's relation to the life insured.
    # Upper-cased and trimmed but otherwise kept verbatim: the vocabulary is the
    # source's, and mapping it to ours here would lose the distinction between a
    # value we did not recognise and one the source left blank.
    if party.relationship_field and party.relationship_field in raw.collect_schema().names():
        stated = (
            pl.col(party.relationship_field).cast(pl.String, strict=False)
            .str.strip_chars().str.to_uppercase()
        )
        stated = pl.when(stated.str.len_chars() > 0).then(stated).otherwise(None)
    else:
        stated = pl.lit(None, dtype=pl.String)

    frame = raw.select(
        *exprs,
        *_passthrough(raw),
        pl.col(party.key_field).cast(pl.String, strict=False).str.strip_chars()
        .alias("source_party_key"),
        pl.lit(party.key_kind).alias("source_key_kind"),
        pl.lit(party.role.value).alias("role"),
        pl.lit(party.role_sequence, dtype=pl.Int16).alias("role_sequence"),
        stated.alias("stated_relationship"),
        pl.lit(mapping.source_system).alias("source_system"),
        N.normalize_policy_number(
            pl.col(_policy_number_source(mapping)).cast(pl.String, strict=False)
        ).alias("policy_number_normalized"),
    )

    # Name derivations. full_name is kept exactly as sourced; everything below
    # is a function of it and is recomputable at any time.
    frame = frame.with_columns(
        N.normalize_full_name(pl.col("full_name")).alias("full_name_normalized"),
        N.extract_name_prefix(N.normalize_text(pl.col("full_name"))).alias("name_prefix_derived"),
        N.extract_name_suffix(N.normalize_text(pl.col("full_name"))).alias("name_suffix_derived"),
    ).with_columns(
        N.name_tokens(pl.col("full_name_normalized")).alias("name_tokens"),
    ).with_columns(
        N.name_sorted_key(pl.col("name_tokens")).alias("name_sorted_key"),
        N.name_phonetic_key(pl.col("full_name_normalized")).alias("name_phonetic_key"),
        N.name_initials(pl.col("name_tokens")).alias("name_initials"),
        N.detect_party_type(pl.col("name_tokens")).alias("party_type"),
    )

    # Component inference. A two-token name splits unambiguously enough to be
    # useful; anything longer is left to the statistical parser and then to the
    # local model, and says so in name_parse_method rather than guessing and
    # presenting the guess as fact.
    two_token = pl.col("name_tokens").list.len() == 2
    is_person = pl.col("party_type") == "PERSON"
    simple = two_token & is_person

    frame = frame.with_columns(
        pl.when(simple).then(pl.col("name_tokens").list.get(0)).alias("given_name_derived"),
        pl.when(simple).then(pl.col("name_tokens").list.get(1)).alias("surname_derived"),
        # Emitted even though the two-token path never populates it. The registry
        # declares this column, and a learned EXTRACT rule that writes it is
        # skipped outright if it is absent -- so omitting it would silently
        # disable every mined rule for names carrying a middle component, which
        # is the largest category there is.
        pl.lit(None, dtype=pl.String).alias("middle_name_derived"),
        pl.when(simple)
        .then(pl.lit(0.75))
        .otherwise(pl.lit(0.0))
        .alias("name_parse_confidence"),
        pl.when(simple)
        .then(pl.lit(NameParseMethod.RULE_BASED.value))
        .otherwise(pl.lit(NameParseMethod.NOT_PARSED.value))
        .alias("name_parse_method"),
    )

    # Contact derivations. The registry marks email_normalized and phone_e164
    # as derived, so a mapping is forbidden from supplying them and the mapping
    # loader rejects any that tries. They are computed here from the raw columns
    # instead, which is what keeps the raw and matchable forms from drifting.
    mapped = {f.canonical for f in party.fields}
    if "email_address" in mapped:
        frame = frame.with_columns(
            N.normalize_email(pl.col("email_address")).alias("email_normalized")
        )
    if "phone_raw" in mapped:
        frame = frame.with_columns(
            N.normalize_phone(pl.col("phone_raw"), mapping.default_country_code)
            .alias("phone_e164")
        )

    # Address derivations, when the block maps any address component.
    address_parts = [c for c in ("address_line1", "address_line2") if c in mapped]
    if address_parts and "postal_code" in mapped:
        frame = frame.with_columns(
            N.normalize_address(*[pl.col(c) for c in address_parts]).alias("address_normalized")
        ).with_columns(
            N.address_key(pl.col("address_normalized"), pl.col("postal_code")).alias("address_key")
        )

    return frame


def _policy_number_source(mapping: SourceMapping) -> str:
    """The inbound column holding the policy number.

    Party rows need it to attach their edge to the right policy, and it lives in
    the policy block rather than in any party block.
    """
    for fm in mapping.policy:
        if fm.canonical == "policy_number" and fm.source:
            return fm.source
    raise ValueError(  # pragma: no cover - guarded by SourceMapping.__post_init__
        f"{mapping.source_system}: policy_number has no source column"
    )


def shred_parties(raw: pl.LazyFrame, mapping: SourceMapping) -> pl.LazyFrame:
    """Project every party block and stack the results.

    ``how="diagonal"`` because blocks legitimately map different attribute sets
    — an agent block rarely carries a date of birth, an owner block usually
    does. Diagonal concatenation fills the gaps with nulls rather than requiring
    every block to declare every column.

    Rows whose party key and name are both empty are dropped: a policy with no
    second insured produces a blank block, and admitting it would create a
    phantom party per policy.
    """
    frames = [_party_frame(raw, party, mapping) for party in mapping.parties]
    stacked = pl.concat(frames, how="diagonal")

    has_key = pl.col("source_party_key").is_not_null() & (
        pl.col("source_party_key").str.len_chars() > 0
    )
    has_name = pl.col("full_name_normalized").str.len_chars() > 0
    return stacked.filter(has_key | has_name)


def collapse_parties(parties: pl.LazyFrame) -> pl.LazyFrame:
    """Collapse repeated party occurrences into one row per source identity.

    This is the deterministic half of entity resolution, and it belongs here
    rather than in the matching stage because it needs no scoring at all: within
    one source system, the same identifier in the same namespace is the same
    party by definition. That is the hybrid identity assumption the model is
    built on, applied at its cheapest.

    Doing it before blocking is not an optimization detail, it is the difference
    between a tractable and an intractable candidate space. An agent appears on
    every policy they wrote, so a book of 200k policies carries 200k agent party
    rows describing a few thousand agents. Blocking over the uncollapsed frame
    would generate candidate pairs for every pair of occurrences of the same
    agent — quadratic work to rediscover something the source already stated.

    Attribute selection is deliberately simple: rows are ordered by how many
    matchable attributes they carry, and each column takes its first non-null
    value in that order. So the most complete occurrence wins, and a column it
    happens to lack is still filled from a less complete sibling. This is
    intra-source consolidation only. Full registry-driven survivorship across
    sources — MOST_RECENT, ANY_TRUE, source trust weights and the provenance
    rows that record each decision — is a later stage, and this does not
    pre-empt it.

    Rows with no source identifier are passed through uncollapsed. A party the
    source declined to identify cannot be deterministically equal to anything,
    and merging such rows on name alone here would be probabilistic matching
    performed in the wrong place and without an audit trail.
    """
    keys = ["source_system", "source_key_kind", "source_party_key"]

    # Policy-scoped columns are dropped rather than collapsed. A party holds
    # different roles on different policies, so "the role" of a collapsed party
    # is not a well-defined value; keeping an arbitrary one would invite code
    # downstream to trust it. That information lives on the edge, which is built
    # from the uncollapsed frame.
    parties = parties.drop(POLICY_SCOPED_COLUMNS, strict=False)

    completeness = sum(
        pl.col(c).is_not_null().cast(pl.Int32)
        for c in ("date_of_birth", "email_normalized", "phone_e164", "address_key", "postal_code")
    )

    identified = parties.filter(
        pl.col("source_party_key").is_not_null()
        & (pl.col("source_party_key").str.len_chars() > 0)
    )
    anonymous = parties.filter(
        pl.col("source_party_key").is_null()
        | (pl.col("source_party_key").str.len_chars() == 0)
    )

    schema = parties.collect_schema().names()
    value_cols = [c for c in schema if c not in keys]

    # Completeness first, then the record id as a tie-break. Two occurrences
    # carrying the same number of matchable attributes are equally good by the
    # rule above, and "first" among them would otherwise mean whichever row the
    # threaded group-by happened to see first — so the same file collapses two
    # ways on two runs and the golden record churns for no stated reason.
    order = ["_completeness"]
    descending = [True]
    if PASSTHROUGH_COLUMNS[0] in schema:
        order.append(PASSTHROUGH_COLUMNS[0])
        descending.append(False)

    collapsed = (
        identified.with_columns(completeness.alias("_completeness"))
        .sort(order, descending=descending)
        .group_by(keys)
        .agg([pl.col(c).drop_nulls().first().alias(c) for c in value_cols])
        .select(schema)
    )

    return pl.concat([collapsed, anonymous], how="vertical")


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------


def shred_relationships(parties: pl.LazyFrame, mapping: SourceMapping) -> pl.LazyFrame:
    """Derive the PARTY_POLICY edges from the shredded party rows.

    Built from the party frame rather than from the raw batch, so an edge exists
    exactly when its party row does and the two cannot disagree about which
    parties a policy has.

    ``from_person_id`` and ``to_policy_id`` are deliberately absent. Edges leave
    the shredder carrying only source keys; the golden writer resolves those to
    surrogate ids inside the same transaction that writes the entities, because
    resolving them here would mean guessing at identity before matching has run.
    """
    return parties.select(
        pl.lit(EdgeKind.PARTY_POLICY.value).alias("edge_kind"),
        pl.col("source_party_key"),
        pl.col("source_key_kind"),
        pl.col("policy_number_normalized"),
        pl.col("role"),
        pl.col("role_sequence"),
        pl.col("stated_relationship"),
        pl.col("source_system"),
        pl.col("full_name_normalized"),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def shred(
    raw: pl.DataFrame | pl.LazyFrame, mapping: SourceMapping
) -> dict[str, pl.DataFrame]:
    """Shred a raw batch into the three canonical frames.

    Validates the batch against the mapping first. A renamed inbound column is
    reported by name here, rather than surfacing later as a canonical column
    that is entirely null for one day's file.

    The three frames are collected together with ``pl.collect_all``, so Polars
    plans them as one job and shares the scan of the raw batch across all three
    rather than reading it once per output.
    """
    lazy = raw.lazy() if isinstance(raw, pl.DataFrame) else raw

    available = lazy.collect_schema().names()
    missing = mapping.missing_columns(available)
    if missing:
        raise ValueError(
            f"{mapping.source_system}: batch is missing mapped columns {list(missing)}. "
            f"Available: {sorted(available)}"
        )

    policies = shred_policies(lazy, mapping)
    occurrences = shred_parties(lazy, mapping)

    # Edges come from the uncollapsed occurrences -- one per party per policy.
    # Person comes from the collapsed frame -- one per source identity. Deriving
    # both from the same intermediate is what stops the two from disagreeing
    # about which parties a policy has.
    relationships = shred_relationships(occurrences, mapping)
    persons = collapse_parties(occurrences)

    collected = pl.collect_all([policies, persons, relationships])
    return {
        "policy": collected[0],
        "person": collected[1],
        "relationship": collected[2],
    }
