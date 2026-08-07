"""Hybrid standardization: vectorized deterministic, quality gate, AI fallback, learning loop.

The AI share of a batch is a number that should fall over time, because work the
model does once becomes a deterministic rule that no model ever repeats.
"""

from cmdm.standardize.agent import (
    MIN_EVIDENCE,
    ShadowResult,
    mine_and_propose,
    mine_signatures,
    propose_rules,
    shadow_evaluate,
)
from cmdm.standardize.ai import (
    HeuristicStandardizer,
    OnnxStandardizer,
    StandardizationRequest,
    StandardizationResult,
    get_standardizer,
)
from cmdm.standardize.gate import CHECKS, apply_gate, gate_summary
from cmdm.standardize.pipeline import StandardizationReport, standardize
from cmdm.standardize.rules import (
    RuleKind,
    RuleState,
    StandardizationRule,
    approve_rule,
    list_rules,
    load_active_rules,
    reject_rule,
)

__all__ = [
    "CHECKS", "apply_gate", "gate_summary",
    "StandardizationRequest", "StandardizationResult", "get_standardizer",
    "HeuristicStandardizer", "OnnxStandardizer",
    "StandardizationRule", "RuleKind", "RuleState",
    "load_active_rules", "list_rules", "approve_rule", "reject_rule",
    "mine_signatures", "propose_rules", "shadow_evaluate", "mine_and_propose",
    "ShadowResult", "MIN_EVIDENCE",
    "standardize", "StandardizationReport",
]
