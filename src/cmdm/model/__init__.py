"""Canonical data model for Customer Master Data Management.

Three entities, declared once in :mod:`cmdm.model.fields` and projected from
there into every representation the system needs:

*   **Policy** - the insurance contract, and the grain the source data arrives
    in. Identified deterministically by policy number within a source system.
*   **Person** - the party, natural or legal, acting in a policy role. Has no
    natural key of its own; identity is carried by the crosswalk.
*   **Relationship** - the role-bearing edge. Sourced Person-to-Policy edges
    carrying Owner, Insured or Agent, plus Person-to-Person edges derived from
    them.

Supporting tables live in :mod:`cmdm.model.control`: the immutable landing zone,
the identity crosswalk, per-attribute survivorship provenance, and the
append-only match audit that holds the AI fallback accountable.
"""

from cmdm.model.control import (
    ANCHORS,
    ATTRIBUTE_PROVENANCE,
    CONTROL_TABLES,
    MATCH_AUDIT,
    PERSON_MASTER,
    PERSON_XREF,
    POLICY_MASTER,
    POLICY_XREF,
    SOURCE_RECORD,
)
from cmdm.model.fields import (
    ENTITIES,
    PERSON,
    POLICY,
    RELATIONSHIP,
    EntitySpec,
    FieldSpec,
    entity,
)

__all__ = [
    "POLICY",
    "PERSON",
    "RELATIONSHIP",
    "ENTITIES",
    "EntitySpec",
    "FieldSpec",
    "entity",
    "ANCHORS",
    "CONTROL_TABLES",
    "PERSON_MASTER",
    "POLICY_MASTER",
    "SOURCE_RECORD",
    "PERSON_XREF",
    "POLICY_XREF",
    "ATTRIBUTE_PROVENANCE",
    "MATCH_AUDIT",
]
