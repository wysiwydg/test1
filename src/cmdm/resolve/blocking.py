"""Candidate generation.

Comparing every pair of parties is quadratic and therefore impossible: 136,000
parties is 9.3 billion pairs. Blocking reduces that to the pairs worth scoring
by requiring candidates to agree on at least one cheap key.

The keys were computed once during ingestion and are read here, never recomputed.
Recall of the whole resolver is bounded by the union of these keys — a true
duplicate that shares no blocking key is invisible to everything downstream, no
matter how good the scorer or the model is. That is why the keys deliberately
span independent signals: name shape, name sound, email, phone, address. A pair
missed by one is usually caught by another.

The join is a self-join on the key column, which Polars executes as a hash join
in parallel. There is no Python loop over blocks; a block of size n contributes
n(n-1)/2 pairs as an expression over the grouped frame.

**Oversized blocks are the failure mode that matters.** One block of 50,000
records contributes 1.25 billion pairs on its own and will exhaust memory long
before the scorer sees it. Such blocks are almost never real: they come from a
placeholder value that normalization should have nulled, or from a genuinely
enormous family of common names. Either way the block is capped and the fact is
reported rather than silently absorbed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import polars as pl

__all__ = [
    "BlockingKey",
    "DEFAULT_KEYS",
    "block_pairs",
    "BlockingReport",
    "MAX_BLOCK_SIZE",
]

#: Blocks larger than this are dropped and reported. A block this size is
#: essentially always a normalization failure rather than a real cluster of
#: duplicates, and scoring it would cost more than the entire rest of the run.
MAX_BLOCK_SIZE = 1000


@dataclass(frozen=True, slots=True)
class BlockingKey:
    """One key parties may be grouped by."""

    name: str
    column: str
    #: Keys differ in how much evidence agreement provides. Sharing an email is
    #: near-conclusive; sharing a set of name initials is very weak. The weight
    #: is not used for blocking itself but is carried through so the scorer can
    #: credit *which* key produced a pair.
    weight: float = 1.0

    def usable(self) -> pl.Expr:
        """Rows whose key is present and non-blank.

        A null or empty key must never form a block. Every record missing an
        email would otherwise land in one enormous block together, which is the
        classic way a blocking pass silently becomes quadratic.
        """
        return pl.col(self.column).is_not_null() & (
            pl.col(self.column).cast(pl.String).str.len_chars() > 0
        )


#: The default key set, spanning independent signals on purpose.
DEFAULT_KEYS: tuple[BlockingKey, ...] = (
    BlockingKey("email", "email_normalized", weight=1.0),
    BlockingKey("phone", "phone_e164", weight=0.9),
    BlockingKey("name_sorted", "name_sorted_key", weight=0.6),
    BlockingKey("name_phonetic", "name_phonetic_key", weight=0.5),
    BlockingKey("address", "address_key", weight=0.7),
)


@dataclass(slots=True)
class BlockingReport:
    """What candidate generation did, and what it refused to do."""

    records: int = 0
    candidate_pairs: int = 0
    pairs_by_key: dict[str, int] = field(default_factory=dict)
    oversized_blocks: list[tuple[str, str, int]] = field(default_factory=list)
    dropped_pairs: int = 0

    @property
    def reduction_ratio(self) -> float:
        """How much of the quadratic space was avoided.

        Reported because it is the number that says whether blocking is working.
        A ratio near 1 means blocking achieved nothing; a very high ratio with
        poor recall means the keys are too strict.
        """
        naive = self.records * (self.records - 1) // 2
        return naive / self.candidate_pairs if self.candidate_pairs else float("inf")


def _pairs_for_key(
    frame: pl.DataFrame, key: BlockingKey, id_column: str, max_block: int
) -> tuple[pl.DataFrame, list[tuple[str, str, int]], int]:
    """Generate the pairs one key contributes.

    Self-join on the key, keeping only ``left < right`` so each unordered pair
    appears once and no record pairs with itself. Doing the deduplication in the
    join predicate rather than afterwards halves the intermediate size, which
    matters because the intermediate is the largest thing this pipeline builds.
    """
    usable = frame.filter(key.usable()).select([id_column, key.column])
    if usable.height == 0:
        return (
            pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String,
                                 "blocking_key": pl.String}),
            [], 0,
        )

    sizes = usable.group_by(key.column).len(name="n")
    oversized = sizes.filter(pl.col("n") > max_block)

    dropped = 0
    reported: list[tuple[str, str, int]] = []
    if oversized.height:
        for row in oversized.iter_rows(named=True):
            n = int(row["n"])
            dropped += n * (n - 1) // 2
            reported.append((key.name, str(row[key.column])[:60], n))
        usable = usable.join(oversized.select(key.column), on=key.column, how="anti")

    pairs = (
        usable.join(usable, on=key.column, how="inner", suffix="_right")
        .rename({id_column: "left_id", f"{id_column}_right": "right_id"})
        .filter(pl.col("left_id") < pl.col("right_id"))
        .select(
            "left_id", "right_id", pl.lit(key.name).alias("blocking_key")
        )
    )
    return pairs, reported, dropped


def block_pairs(
    frame: pl.DataFrame,
    *,
    keys: Sequence[BlockingKey] = DEFAULT_KEYS,
    id_column: str = "person_id",
    max_block: int = MAX_BLOCK_SIZE,
) -> tuple[pl.DataFrame, BlockingReport]:
    """Generate candidate pairs from a party frame.

    Returns a frame of ``(left_id, right_id, blocking_key)`` with one row per
    unordered pair, and a report. A pair found by several keys appears once, with
    the keys concatenated — which the scorer uses, since agreeing on both email
    and address is stronger evidence than agreeing on either.
    """
    report = BlockingReport(records=frame.height)
    if frame.height < 2:
        return (
            pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String,
                                 "blocking_keys": pl.String}),
            report,
        )

    available = set(frame.columns)
    frames: list[pl.DataFrame] = []

    for key in keys:
        if key.column not in available:
            continue
        pairs, oversized, dropped = _pairs_for_key(frame, key, id_column, max_block)
        report.pairs_by_key[key.name] = pairs.height
        report.oversized_blocks.extend(oversized)
        report.dropped_pairs += dropped
        if pairs.height:
            frames.append(pairs)

    if not frames:
        return (
            pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String,
                                 "blocking_keys": pl.String}),
            report,
        )

    combined = (
        pl.concat(frames, how="vertical")
        .group_by(["left_id", "right_id"])
        .agg(pl.col("blocking_key").unique().sort().str.join("+").alias("blocking_keys"))
    )
    report.candidate_pairs = combined.height
    return combined, report
