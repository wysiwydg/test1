"""Graph merging and Master ID assignment.

Matching decides about *pairs*. Identity is about *sets*. If A matches B and B
matches C, then A, B and C are one party even though A and C may never have been
compared — they might share no blocking key at all. Resolving that transitivity
is what turns a list of matched pairs into master records.

The pairs form an undirected graph; the answer is its connected components. That
is computed with ``scipy.sparse.csgraph.connected_components`` over a CSR
adjacency matrix, which is a compiled union-find over the whole edge set at once.
A Python graph traversal over millions of edges would dominate the run; this is
milliseconds.

**Transitivity is a genuine risk, not just a convenience.** Chains merge
aggressively: a single wrong edge silently unions two otherwise unrelated
clusters, and the damage is proportional to the size of both. Two controls apply:

*   Only edges the pipeline actually accepted become graph edges — auto-matches
    and AI-approved grey-zone pairs. Rejected pairs are recorded but contribute
    nothing.
*   Components above a size threshold are flagged rather than trusted. A cluster
    of 400 "people" is not a family, it is a bad edge, and it is far cheaper to
    catch it here than to explain it to a regulator later.

Master IDs are assigned deterministically: the smallest member id wins. Running
the clustering twice over the same edges therefore produces the same master ids,
which is what makes a resolution run reproducible and re-runnable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import polars as pl

__all__ = [
    "ClusterResult",
    "cluster_pairs",
    "SUSPICIOUS_CLUSTER_SIZE",
]

#: Components at or above this size are reported for review rather than accepted
#: silently. Real duplicate clusters in party data are small -- a person has two
#: or three records, occasionally a dozen. Hundreds means a bad edge chained
#: unrelated groups together.
SUSPICIOUS_CLUSTER_SIZE = 25


@dataclass(slots=True)
class ClusterResult:
    """The outcome of resolving pairs into master records."""

    #: person_id -> master_id.
    assignments: pl.DataFrame
    cluster_count: int = 0
    merged_records: int = 0
    largest_cluster: int = 0
    suspicious_clusters: list[tuple[str, int]] = field(default_factory=list)
    singletons: int = 0

    @property
    def collapse_ratio(self) -> float:
        """Records per master record.

        The headline resolution number: 1.0 means nothing merged, 2.0 means the
        population halved.
        """
        total = self.assignments.height
        return total / self.cluster_count if self.cluster_count else 1.0


def cluster_pairs(
    accepted_pairs: pl.DataFrame,
    all_ids: Sequence[str] | pl.Series,
    *,
    suspicious_size: int = SUSPICIOUS_CLUSTER_SIZE,
) -> ClusterResult:
    """Resolve accepted pairs into connected components and assign master ids.

    ``accepted_pairs`` must contain only edges the pipeline accepted. Every id in
    ``all_ids`` receives a master id, including those in no pair at all — a party
    with no duplicates is a cluster of one, and omitting it would mean the
    resolution output did not cover the population.

    Master id is the lexicographically smallest member, so the assignment is
    deterministic and a re-run over identical edges reproduces it exactly.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    ids = pl.Series("person_id", all_ids, dtype=pl.String).unique().sort()
    n = ids.len()

    if n == 0:
        return ClusterResult(
            assignments=pl.DataFrame(
                schema={"person_id": pl.String, "master_id": pl.String}
            )
        )

    # Dense integer indices for the sparse matrix. The lookup frame is built once
    # and joined, rather than a Python dict comprehension over millions of ids.
    index = pl.DataFrame({"person_id": ids}).with_row_index("idx")

    if accepted_pairs.height:
        edges = (
            accepted_pairs.select("left_id", "right_id")
            .join(index.rename({"person_id": "left_id", "idx": "left_idx"}), on="left_id")
            .join(index.rename({"person_id": "right_id", "idx": "right_idx"}), on="right_id")
        )
        rows = edges["left_idx"].to_numpy()
        cols = edges["right_idx"].to_numpy()
    else:
        rows = np.array([], dtype=np.int64)
        cols = np.array([], dtype=np.int64)

    data = np.ones(len(rows), dtype=np.int8)
    graph = coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()

    # directed=False so an edge is traversable both ways; the matrix only holds
    # the upper triangle because pairs are stored with left < right.
    count, labels = connected_components(graph, directed=False, return_labels=True)

    assigned = index.with_columns(pl.Series("label", labels))

    # Master id = smallest member id in the component. Deterministic, and it
    # keeps a stable id when a cluster later gains members.
    masters = assigned.group_by("label").agg(
        pl.col("person_id").min().alias("master_id"),
        pl.len().alias("cluster_size"),
    )
    assignments = (
        assigned.join(masters, on="label")
        .select("person_id", "master_id", "cluster_size")
    )

    sizes = masters["cluster_size"]
    suspicious = masters.filter(pl.col("cluster_size") >= suspicious_size)

    return ClusterResult(
        assignments=assignments.select("person_id", "master_id"),
        cluster_count=int(count),
        merged_records=int(assignments.filter(pl.col("cluster_size") > 1).height),
        largest_cluster=int(sizes.max() or 0),
        singletons=int(masters.filter(pl.col("cluster_size") == 1).height),
        suspicious_clusters=[
            (str(r["master_id"]), int(r["cluster_size"]))
            for r in suspicious.sort("cluster_size", descending=True).iter_rows(named=True)
        ],
    )
