"""Observability, kept separate from anything business-facing.

Three audiences with three questions: is the pipeline keeping up (operational),
is resolution correct (match quality), is the data any good (data quality).
Mixing them produces a dashboard that serves none of them.
"""

from cmdm.observe.metrics import (
    CRITICAL_PERSON_FIELDS,
    DataQuality,
    MatchQuality,
    OperationalSnapshot,
    assess_data_quality,
    evaluate_matching,
    operational_snapshot,
    registry,
    render_prometheus,
)

__all__ = [
    "MatchQuality", "evaluate_matching",
    "DataQuality", "assess_data_quality", "CRITICAL_PERSON_FIELDS",
    "OperationalSnapshot", "operational_snapshot",
    "registry", "render_prometheus",
]
