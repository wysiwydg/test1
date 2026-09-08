"""The rule catalogue.

A registry rather than a list of imports in the engine, for one operational
reason: the catalogue is a document. ``rule_catalogue()`` renders every rule
with its identifier, version, the statutory circumstance it evidences and its
tunable parameters, and that output belongs in the institution's AML manual and
in front of an examiner asking what the system actually looks for.

Rule identifiers are namespaced by what they produce — ``ctr.*`` detects covered
transactions, ``str.*`` detects suspicion — because those two go down different
paths: a covered transaction is filed because of what it is, a suspicious one
only after a human determines it is.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from aml.config import AmlConfig
from aml.rules.base import Rule

__all__ = ["register", "all_rules", "active_rules", "rule_catalogue", "get_rule"]

_REGISTRY: dict[str, Rule] = {}

#: The modules that define rules. Imported on first use rather than from the
#: package's ``__init__``: importing them there would mean the rule modules and
#: the package that holds them import each other, which works at runtime and
#: defeats static analysis.
_RULE_MODULES = ("covered", "structuring", "insurance", "profile", "geography", "cdd")
_loaded = False

R = TypeVar("R", bound=type)


def _load_rules() -> None:
    """Import the rule modules, which registers them."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in _RULE_MODULES:
        importlib.import_module(f"aml.rules.{name}")


def register(cls: R) -> R:
    """Class decorator adding a rule to the catalogue."""
    instance = cls()
    rule_id = getattr(instance, "rule_id", "")
    if not rule_id:
        raise ValueError(f"{cls.__name__} has no rule_id")
    if rule_id in _REGISTRY:
        raise ValueError(f"duplicate rule_id {rule_id!r} ({cls.__name__})")
    _REGISTRY[rule_id] = instance
    return cls


def all_rules() -> Sequence[Rule]:
    _load_rules()
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def get_rule(rule_id: str) -> Rule | None:
    _load_rules()
    return _REGISTRY.get(rule_id)


def active_rules(config: AmlConfig) -> Sequence[Rule]:
    """Rules enabled by configuration, in a stable order."""
    return tuple(r for r in all_rules() if config.rules.is_enabled(r.rule_id))


def rule_catalogue() -> list[Mapping[str, Any]]:
    """Every registered rule, as printable data."""
    return [
        {
            "rule_id": rule.rule_id,
            "version": rule.version,
            "title": rule.title,
            "description": rule.description,
            "st_codes": list(rule.st_codes),
            "parameters": dict(rule.default_params),
        }
        for rule in all_rules()
    ]
