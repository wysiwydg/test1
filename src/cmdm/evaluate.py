"""Match quality, measured against ground truth rather than asserted.

Thresholds are the most consequential numbers in the system and the easiest to
change casually. Moving auto-match from 0.85 to 0.80 merges more pairs, which
looks like an improvement in every metric the consoles show -- more parties
resolved, fewer duplicates, a tidier book -- right up until somebody's policy
is attached to a stranger. Nothing in the test suite catches that today,
because "did resolution do the right thing" has no answer without knowing what
the right thing was.

This module supplies the answer key. The sample generator can emit an extract
where a share of parties arrive under two customer numbers, and it knows which
pairs those are. Running the pipeline over that extract and comparing what it
merged against what it should have merged gives precision and recall that mean
something.

Four measurements, because a single number hides where the loss is:

*   **Blocking recall.** Of the true duplicate pairs, how many even became
    candidates. A pair that blocking never generated is invisible to everything
    downstream -- no comparator, no model, no steward will ever see it -- so
    this is a ceiling on everything below and the one worth checking first.
*   **Pair precision and recall.** Of the pairs finally merged, how many should
    have been; of the pairs that should have been, how many were.
*   **What the model contributed.** How many true positives came out of the
    grey zone, and how many false positives it introduced. The grey zone exists
    to buy recall the deterministic pass cannot reach; if it is buying nothing,
    the model is cost without benefit, and if it is buying recall at the price
    of precision that is a threshold problem rather than a model problem.
*   **Cluster damage.** Merging is transitive, so one wrong edge can join two
    clusters that share no pair at all. Pair precision misses that entirely --
    the bad pair is one row -- while the damage is proportional to the size of
    both clusters.

Deliberately not a test fixture. It runs against a real store on demand
(``worker evaluate``) so the same numbers can be produced on an operator's
machine from their own data, and the test suite calls the same functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

__all__ = [
    "MatchQuality",
    "evaluate_matching",
    "keys_by_person",
    "SEPARATOR",
]

#: The unit separator the pipeline joins a source identity with. Split back
#: apart here rather than re-deriving, so the harness reads exactly what the
#: pipeline wrote.
SEPARATOR = "\x1f"


@dataclass(slots=True)
class MatchQuality:
    """What one evaluation found. Every field is a count or a ratio."""

    truth_pairs: int = 0
    #: True pairs whose two sides both reached the store at all. A pair whose
    #: party never landed is not the matcher's failure and is excluded rather
    #: than counted against it.
    evaluable_pairs: int = 0

    candidates: int = 0
    blocked_pairs: int = 0
    blocking_recall: float = 0.0

    merged_pairs: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0

    ai_true_positives: int = 0
    ai_false_positives: int = 0
    ai_share_of_true_positives: float = 0.0

    clusters_with_a_wrong_member: int = 0
    largest_wrong_cluster: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "truth_pairs": self.truth_pairs,
            "evaluable_pairs": self.evaluable_pairs,
            "candidates": self.candidates,
            "blocked_pairs": self.blocked_pairs,
            "blocking_recall": round(self.blocking_recall, 4),
            "merged_pairs": self.merged_pairs,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "ai_true_positives": self.ai_true_positives,
            "ai_false_positives": self.ai_false_positives,
            "ai_share_of_true_positives": round(self.ai_share_of_true_positives, 4),
            "clusters_with_a_wrong_member": self.clusters_with_a_wrong_member,
            "largest_wrong_cluster": self.largest_wrong_cluster,
            "examples": self.examples,
        }

    def summary(self) -> str:
        return (
            f"precision {self.precision:.3f}  recall {self.recall:.3f}  "
            f"F1 {self.f1:.3f}  (blocking recall {self.blocking_recall:.3f}, "
            f"{self.evaluable_pairs:,} pairs to find)"
        )


def _source_key(identity: str) -> str:
    """The source key out of a ``system\\x1fkind\\x1fkey`` identity."""
    return identity.rsplit(SEPARATOR, 1)[-1]


def keys_by_person(conn: psycopg.Connection) -> dict[str, str]:
    """Source key to the golden party it resolved to.

    Read from the crosswalk rather than reconstructed, because the crosswalk is
    what the rest of the system will use to answer the same question.
    """
    rows = conn.execute(
        "SELECT source_party_key, person_id::text FROM mdm.person_xref "
        "WHERE is_active"
    ).fetchall()
    return {key: person for key, person in rows}


def evaluate_matching(
    conn: psycopg.Connection,
    truth_pairs: set[tuple[str, str]],
    *,
    examples: int = 5,
) -> MatchQuality:
    """Score what the store actually merged against what it should have.

    ``truth_pairs`` is the set of source-key pairs naming one real party,
    normally from ``scripts.generate_sample_data.duplicate_pairs``. Any pair
    whose sides did not both reach the store is dropped from the denominator:
    a party that never landed is an ingestion question, and counting it here
    would move match quality for reasons that have nothing to do with matching.
    """
    report = MatchQuality(truth_pairs=len(truth_pairs))
    resolved = keys_by_person(conn)

    evaluable = {
        (left, right) for left, right in truth_pairs
        if left in resolved and right in resolved
    }
    report.evaluable_pairs = len(evaluable)
    if not evaluable:
        return report

    # -- what the matcher merged, as source-key pairs.
    ledger = conn.execute(
        """
        SELECT left_source_identity, right_source_identity, zone,
               final_decision, ai_decision
        FROM mdm.match_pair
        WHERE left_source_identity IS NOT NULL
          AND right_source_identity IS NOT NULL
        """
    ).fetchall()
    report.candidates = len(ledger)

    candidate_pairs: set[tuple[str, str]] = set()
    merged: set[tuple[str, str]] = set()
    merged_by_ai: set[tuple[str, str]] = set()

    for left_identity, right_identity, zone, final, ai_decision in ledger:
        pair = tuple(sorted((_source_key(left_identity),
                             _source_key(right_identity))))
        candidate_pairs.add(pair)
        if final == "MATCH":
            merged.add(pair)
            if zone == "GREY" and ai_decision == "MATCH":
                merged_by_ai.add(pair)

    report.blocked_pairs = len(evaluable & candidate_pairs)
    report.blocking_recall = report.blocked_pairs / len(evaluable)

    # -- a pair also counts as merged when both sides landed on one golden
    #    party, which is how a deterministic collapse or a transitive merge
    #    joins two keys without any explicit pair being accepted.
    same_party = {
        pair for pair in evaluable
        if resolved[pair[0]] == resolved[pair[1]]
    }
    merged_true = same_party | (merged & evaluable)

    report.merged_pairs = len(merged)
    report.true_positives = len(merged_true)
    report.false_negatives = len(evaluable) - report.true_positives
    report.false_positives = len(merged - evaluable)

    decided = report.true_positives + report.false_positives
    report.precision = report.true_positives / decided if decided else 1.0
    report.recall = report.true_positives / len(evaluable)
    if report.precision + report.recall:
        report.f1 = (2 * report.precision * report.recall
                     / (report.precision + report.recall))

    report.ai_true_positives = len(merged_by_ai & evaluable)
    report.ai_false_positives = len(merged_by_ai - evaluable)
    if report.true_positives:
        report.ai_share_of_true_positives = (
            report.ai_true_positives / report.true_positives
        )

    report.clusters_with_a_wrong_member, report.largest_wrong_cluster = (
        _cluster_damage(conn, truth_pairs, resolved)
    )
    report.examples = _examples(conn, merged - evaluable, limit=examples)
    return report


def _cluster_damage(
    conn: psycopg.Connection,
    truth_pairs: set[tuple[str, str]],
    resolved: dict[str, str],
) -> tuple[int, int]:
    """Golden parties holding source keys that name different real people.

    The measurement pair precision cannot make. Merging is transitive, so one
    accepted edge between two clusters joins every member of both -- and the
    ledger records that as a single bad pair while the store records it as
    dozens of people who are now one person.
    """
    # Canonical real-person id per source key: the smallest key it is tied to.
    real: dict[str, str] = {}
    for left, right in truth_pairs:
        anchor = min(left, right)
        real[left] = min(real.get(left, anchor), anchor)
        real[right] = min(real.get(right, anchor), anchor)

    members: dict[str, set[str]] = {}
    for key, person in resolved.items():
        members.setdefault(person, set()).add(real.get(key, key))

    wrong = [people for people in members.values() if len(people) > 1]
    return len(wrong), max((len(p) for p in wrong), default=0)


def _examples(
    conn: psycopg.Connection,
    false_positives: set[tuple[str, str]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """A few wrong merges, with the names as delivered, so a number becomes a case.

    A report that says precision fell to 0.94 tells somebody to go looking. A
    report naming the two parties tells them what to look at.

    The names come from the landing zone, not from the golden record. Reading
    them from ``mdm.person`` prints the *surviving* name twice -- the two sides
    have already been merged, that is the finding -- which reads as though the
    two records were identical and makes a wrong merge look reasonable.
    """
    out: list[dict[str, Any]] = []
    for left, right in sorted(false_positives)[:limit]:
        example: dict[str, Any] = {
            "left": _delivered_name(conn, left),
            "right": _delivered_name(conn, right),
        }
        row = conn.execute(
            """
            SELECT score, zone, ai_score, model_name
            FROM mdm.match_pair
            WHERE (left_source_identity LIKE %s AND right_source_identity LIKE %s)
               OR (left_source_identity LIKE %s AND right_source_identity LIKE %s)
            LIMIT 1
            """,
            (f"%{SEPARATOR}{left}", f"%{SEPARATOR}{right}",
             f"%{SEPARATOR}{right}", f"%{SEPARATOR}{left}"),
        ).fetchone()
        if row is None:
            # No accepted pair joins them, so they were pulled together
            # transitively -- through some third record that matched both.
            example["how"] = "transitive: no pair between these two was accepted"
        else:
            score, zone, ai_score, model = row
            example |= {
                "score": round(float(score), 3) if score is not None else None,
                "zone": zone,
                "ai_score": round(float(ai_score), 3) if ai_score is not None else None,
                "model": model,
            }
        out.append(example)
    return out


def _delivered_name(conn: psycopg.Connection, key: str) -> str:
    """The name a source key arrived with, straight out of the landing zone."""
    # The name from the column the key was found in. A coalesce across the three
    # name columns returns whichever is populated first, which for an agent key
    # is the owner's name -- a wrong name attached to the right key is worse
    # than no name, because it sends the reader after the wrong record.
    row = conn.execute(
        """
        SELECT CASE
                 WHEN payload->>'OwnerCustomerId'   = %(k)s THEN payload->>'OwnerName'
                 WHEN payload->>'InsuredCustomerId' = %(k)s THEN payload->>'InsuredName'
                 WHEN payload->>'AgentCode'         = %(k)s THEN payload->>'AgentName'
               END
        FROM mdm.source_record
        WHERE payload->>'OwnerCustomerId' = %(k)s
           OR payload->>'InsuredCustomerId' = %(k)s
           OR payload->>'AgentCode' = %(k)s
        LIMIT 1
        """,
        {"k": key},
    ).fetchone()
    return f"{key} {row[0]!r}" if row and row[0] else key
