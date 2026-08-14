"""Match quality, asserted against ground truth.

Thresholds are the most consequential numbers in the system and the easiest to
change casually. Lowering auto-match merges more parties, which improves every
figure the consoles show — more resolved, fewer duplicates, a tidier book —
right up until somebody's policy is attached to a stranger. Nothing else in this
suite catches that, because "did resolution do the right thing" has no answer
without knowing what the right thing was.

So this module builds an extract whose answer key is known: a share of parties
arrive under two customer numbers, with the second registration missing a date
of birth or carrying a different email, the way a real re-registration does. The
generator knows which pairs those are. Everything here compares what the
pipeline merged against what it should have.

**The floors are deliberately below the measured baseline.** They exist to catch
a change that breaks matching, not to pin the numbers where they happen to sit —
a floor set at the current value fails on noise and gets raised until it means
nothing. Measured when written, at 2,000 rows: blocking recall 0.934, precision
0.936, recall 0.721, F1 0.815.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest

from cmdm.evaluate import evaluate_matching
from scripts.generate_sample_data import duplicate_pairs, generate

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Small enough to process inside a test, large enough to carry ~60 findable
#: duplicate pairs. Below about a thousand rows the population is too small for
#: the numbers to mean anything.
ROWS = 2000
DUPLICATE_RATE = 0.18

# --- floors. Each is a claim about what must keep working. -------------------

#: A pair blocking never generates is invisible to every stage after it: no
#: comparator, no model and no steward will ever see it. This is the ceiling on
#: recall, so it is checked first and held highest.
MIN_BLOCKING_RECALL = 0.88

#: A wrong merge is worse than a missed one — an unmerged duplicate is visible
#: and fixable, a wrongly merged pair silently destroys two records — so
#: precision is held tighter than recall.
MIN_PRECISION = 0.85
MIN_RECALL = 0.60
MIN_F1 = 0.72

#: Merging is transitive: one wrong edge joins two clusters entirely. A pair
#: metric cannot see that, because the bad pair is a single row while the damage
#: is proportional to the size of both clusters.
MAX_WRONG_CLUSTER = 4


@pytest.fixture(scope="module")
def benchmark_rows() -> pl.DataFrame:
    return pl.DataFrame(generate(ROWS, duplicate_rate=DUPLICATE_RATE))


@pytest.fixture(scope="module")
def truth() -> set[tuple[str, str]]:
    return duplicate_pairs(ROWS, duplicate_rate=DUPLICATE_RATE)


@pytest.fixture
def quality(conn, benchmark_rows, truth):
    """Land and process the benchmark, then score it."""
    from cmdm.ingest.landing import accept_batch
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import process_batch

    # The landing zone *and* the derived store. Truncating source_record alone
    # leaves the golden tables holding whatever was processed before, and the
    # benchmark then gets scored against a store that also contains a different
    # extract's parties -- which changes blocking, clustering and every number
    # below it. In the repository this suite usually meets an empty database and
    # the omission is invisible; run inside a bundle, where `verify` has already
    # processed the reference extract into the same database, blocking recall
    # falls from 0.93 to 0.76 and the failure looks like a matching regression
    # rather than a dirty fixture.
    conn.execute(
        "TRUNCATE mdm.source_record, mdm.relationship, mdm.person, mdm.policy, "
        "mdm.person_master, mdm.policy_master, mdm.person_xref, "
        "mdm.policy_xref, mdm.attribute_provenance, mdm.match_pair CASCADE"
    )
    mapping = load_mapping(REPO / "src" / "cmdm" / "mappings" / "life_admin.toml")
    batch_id, report, enqueued = accept_batch(
        conn, benchmark_rows, mapping, origin="BENCH", filename="bench.csv",
        submitted_by="pytest",
    )
    assert report.accepted and enqueued
    process_batch(conn, batch_id)
    return evaluate_matching(conn, truth)


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_the_benchmark_contains_duplicates_to_find() -> None:
    """Without a second customer number per party there is nothing for the
    probabilistic pass to do: one customer-number space means a party is matched
    deterministically before scoring runs."""
    assert len(duplicate_pairs(ROWS, duplicate_rate=DUPLICATE_RATE)) > 40


def test_the_shipped_extract_has_no_injected_duplicates() -> None:
    """The default must stay exactly what is documented and shipped."""
    assert duplicate_pairs(ROWS, duplicate_rate=0.0) == set()


def test_legal_entity_names_are_unique() -> None:
    """Two companies with one registered name is not a hard case for a matcher,
    it is an impossible one — and it dragged measured precision down by 14
    points for a reason that had nothing to do with matching."""
    import random

    from scripts.generate_sample_data import _build_population

    population = _build_population(
        random.Random(20240807), households_wanted=ROWS // 6,
        duplicate_rate=DUPLICATE_RATE,
    )
    names = [entity["name"] for entity in population["entities"]]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# The floors
# ---------------------------------------------------------------------------


def test_blocking_finds_the_pairs_worth_scoring(quality) -> None:
    assert quality.evaluable_pairs > 30, "too few pairs to measure anything"
    assert quality.blocking_recall >= MIN_BLOCKING_RECALL, (
        f"blocking recall {quality.blocking_recall:.3f} — "
        f"{quality.evaluable_pairs - quality.blocked_pairs} true duplicates "
        "never became candidates, so nothing downstream could have found them"
    )


def test_precision_does_not_regress(quality) -> None:
    assert quality.precision >= MIN_PRECISION, (
        f"precision {quality.precision:.3f} with "
        f"{quality.false_positives} wrong merges. Examples: {quality.examples}"
    )


def test_recall_does_not_regress(quality) -> None:
    assert quality.recall >= MIN_RECALL, (
        f"recall {quality.recall:.3f} — {quality.false_negatives} of "
        f"{quality.evaluable_pairs} true duplicates were not merged"
    )


def test_f1_does_not_regress(quality) -> None:
    assert quality.f1 >= MIN_F1, f"F1 {quality.f1:.3f}"


def test_no_cluster_swallows_several_real_people(quality) -> None:
    """The failure a pair metric cannot see."""
    assert quality.largest_wrong_cluster <= MAX_WRONG_CLUSTER, (
        f"a golden party holds keys naming {quality.largest_wrong_cluster} "
        "different real people"
    )


# ---------------------------------------------------------------------------
# What the model is buying
# ---------------------------------------------------------------------------


def test_the_grey_zone_model_earns_its_place(quality) -> None:
    """The grey zone exists to buy recall the deterministic pass cannot reach.
    If the model contributes no true positives it is cost without benefit, and
    the honest response is to remove it rather than to keep paying for it."""
    assert quality.ai_true_positives > 0, (
        "the model merged nothing that was actually a duplicate"
    )


def test_the_model_is_not_buying_recall_with_precision(quality) -> None:
    """A model that accepts everything would score perfect recall. The check is
    that its true positives outnumber the false ones it introduces."""
    assert quality.ai_true_positives > quality.ai_false_positives, (
        f"the model added {quality.ai_false_positives} wrong merges for "
        f"{quality.ai_true_positives} right ones"
    )


def test_evaluation_reports_nothing_when_there_is_nothing_to_measure(
    conn, benchmark_rows
) -> None:
    """Run against a store with no injected duplicates it must say so, not
    report a precision of zero and look like a failure."""
    empty = evaluate_matching(conn, set())
    assert empty.evaluable_pairs == 0
    assert empty.precision == 0.0 and empty.recall == 0.0
