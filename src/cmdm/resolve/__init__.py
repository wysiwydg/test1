"""Identity resolution: blocking, tri-zone scoring, cross-encoder, graph merge.

The grey zone is the design. A binary threshold forces a call on every pair
including the ones the evidence cannot support; a three-way split lets the cheap
scorer decline, and routes only the declined pairs to a model.
"""

from cmdm.resolve.blocking import DEFAULT_KEYS, BlockingKey, BlockingReport, block_pairs
from cmdm.resolve.clustering import SUSPICIOUS_CLUSTER_SIZE, ClusterResult, cluster_pairs
from cmdm.resolve.crossencoder import (
    AI_ACCEPT_THRESHOLD,
    NICKNAMES,
    CrossEncoder,
    FeatureCrossEncoder,
    OnnxCrossEncoder,
    PairDecision,
    classify_grey_zone,
)
from cmdm.resolve.pipeline import ResolutionReport, persist_run, resolve
from cmdm.resolve.scoring import (
    AUTO_MATCH_THRESHOLD,
    AUTO_REJECT_THRESHOLD,
    COMPARATORS,
    VETOES,
    Comparator,
    ScoringReport,
    Zone,
    attach_attributes,
    score_pairs,
)

__all__ = [
    "BlockingKey", "DEFAULT_KEYS", "block_pairs", "BlockingReport",
    "Comparator", "COMPARATORS", "VETOES", "Zone", "score_pairs", "ScoringReport",
    "AUTO_MATCH_THRESHOLD", "AUTO_REJECT_THRESHOLD", "attach_attributes",
    "CrossEncoder", "FeatureCrossEncoder", "OnnxCrossEncoder", "PairDecision",
    "classify_grey_zone", "AI_ACCEPT_THRESHOLD", "NICKNAMES",
    "cluster_pairs", "ClusterResult", "SUSPICIOUS_CLUSTER_SIZE",
    "resolve", "ResolutionReport", "persist_run",
]
