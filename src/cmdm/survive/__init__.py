"""Survivorship: one golden value per attribute, with its lineage.

The policy lives in the field registry, not here. This module reads the declared
strategies and applies them as one vectorized group-by, and records why each
value won.
"""

from cmdm.survive.engine import (
    CONTRIBUTOR_COLUMN,
    RECENCY_COLUMN,
    TRUST_COLUMN,
    SourceTrust,
    SurvivorshipReport,
    strategy_expression,
    survive,
)

__all__ = [
    "survive", "SourceTrust", "SurvivorshipReport", "strategy_expression",
    "CONTRIBUTOR_COLUMN", "RECENCY_COLUMN", "TRUST_COLUMN",
]
