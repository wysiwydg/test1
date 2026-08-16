"""Everything known about one party, assembled once for every view of it.

The console asks "show me this customer", and it is the same question the API
answers. Writing the SQL twice would mean two chances for them to disagree about
what a customer *is*, and the one that disagreed quietly would be the one nobody
checked.

So the queries live here and the views render what they return. Nothing in this
module produces markup or knows what is calling it -- which is also what let the
UI in front of it be replaced without touching a line of this file or any of its
tests.

**What a 360 has to contain to earn the name.** The golden record on its own is
the least interesting part -- it is what the old page showed, and it answers
"what do we believe" while leaving "why" and "what is this party connected to"
unanswered. A party in an insurance book is a node in a graph:

    Person ──role──> Policy <──role── other parties on that policy
       │                                       (the linkage that
       ├──stated family──> Person               makes a book a book,
       ├──affiliation────> Trust / Company      and the thing no
       │                                        single-entity view
       ├──xref───────────> source keys          can ever show)
       ├──provenance─────> which source won
       └──match ledger───> who we merged, and who we deliberately did not

The last one matters more than it looks. Every other panel shows what the system
concluded; the match ledger shows what it *considered* -- including the parties
it decided were somebody else. "Why is this person not merged with that one" is
asked as often as the opposite, and it is unanswerable from a page that only
shows conclusions.

PII masking is deliberately *not* applied here. Masking is an authorization
decision and belongs where the principal is known; a data layer that masked on
its own would be one that cannot be reused by an export the caller is entitled
to. Each view masks what it renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

__all__ = [
    "Customer360",
    "PolicyRole",
    "load_customer_360",
    "relation_label",
    "ROLE_GROUPS",
]


#: Roles grouped by what the party *is* on the policy, because the three
#: groups answer different questions and are read by different people. An
#: agent's 200 policies are a book; an owner's three are a relationship. Listing
#: them in one undifferentiated table makes a busy agent look like a customer
#: with 200 contracts, which is the single most misleading thing this page
#: could do.
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "owns": ("OWNER", "JOINT_OWNER"),
    "insured": ("INSURED", "JOINT_INSURED"),
    "services": ("AGENT", "WRITING_AGENT", "SERVICING_AGENT"),
    "benefits": ("BENEFICIARY", "CONTINGENT_BENEFICIARY"),
    "pays": ("PAYER", "PAYOR_BANK"),
    "other": ("ASSIGNEE", "UNKNOWN"),
}

_GROUP_OF = {
    role: group for group, roles in ROLE_GROUPS.items() for role in roles
}

_RELATION_LABEL = {
    ("SPOUSE_OF", True): "spouse of",
    ("SPOUSE_OF", False): "spouse of",
    ("CHILD_OF", True): "child of",
    ("CHILD_OF", False): "parent of",
    ("PARENT_OF", True): "parent of",
    ("PARENT_OF", False): "child of",
    ("HOUSEHOLD_MEMBER", True): "same household",
    ("HOUSEHOLD_MEMBER", False): "same household",
    ("EMPLOYEE_OF", True): "employee of",
    ("EMPLOYEE_OF", False): "employs",
    ("TRUST_MEMBER_OF", True): "member of trust",
    ("TRUST_MEMBER_OF", False): "has trust member",
    ("ESTATE_SUBJECT_OF", True): "subject of estate",
    ("ESTATE_SUBJECT_OF", False): "estate of",
    ("CO_OWNER", True): "co-owner with",
    ("CO_OWNER", False): "co-owner with",
    ("CO_INSURED", True): "co-insured with",
    ("CO_INSURED", False): "co-insured with",
    ("OWNER_OF_INSURED", True): "owns cover on",
    ("OWNER_OF_INSURED", False): "insured under policy owned by",
    ("INSURED_OF_OWNER", True): "insured under policy owned by",
    ("INSURED_OF_OWNER", False): "owns cover on",
    ("SERVICED_BY_AGENT", True): "serviced by",
    ("SERVICED_BY_AGENT", False): "services",
    ("AGENT_SERVICES", True): "services",
    ("AGENT_SERVICES", False): "serviced by",
}


def relation_label(association: str, outgoing: bool) -> str:
    """A stated relationship, phrased from this party's side.

    Direction is not cosmetic: "parent of" and "child of" are the same stored
    edge read from opposite ends, and rendering the stored direction regardless
    of which party is being viewed makes a page confidently state that a woman
    is her own daughter's child.
    """
    return _RELATION_LABEL.get(
        (association, outgoing), association.replace("_", " ").lower()
    )


@dataclass(frozen=True, slots=True)
class PolicyRole:
    """One policy this party is on, and what they are on it."""

    policy_id: str
    policy_number: str
    source_system: str
    product_name: str | None
    product_line: str | None
    policy_status: str | None
    currency_code: str | None
    sum_assured_amount: Any | None
    annual_premium_amount: Any | None
    effective_date: Any | None
    termination_date: Any | None
    role: str
    role_group: str
    #: Everyone else on this policy, so the page shows the connection rather
    #: than only the endpoint. This is the difference between "three policies"
    #: and "three policies, two of which cover his wife".
    counterparties: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Customer360:
    """One party and everything the store links to it."""

    person_id: str
    requested_id: str
    record: dict[str, Any]
    policies: list[PolicyRole]
    household_id: str | None
    household_members: list[dict[str, Any]]
    affiliations: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    lineage: list[dict[str, Any]]
    versions: list[dict[str, Any]]
    merged_from: list[dict[str, Any]]
    considered: list[dict[str, Any]]

    @property
    def was_merged_id(self) -> bool:
        """Whether the caller followed an id that has since been merged away."""
        return str(self.requested_id) != str(self.person_id)

    def by_group(self, group: str) -> list[PolicyRole]:
        return [p for p in self.policies if p.role_group == group]

    @property
    def groups_present(self) -> list[str]:
        """Role groups this party actually has, in a stable order."""
        present = {p.role_group for p in self.policies}
        return [g for g in ROLE_GROUPS if g in present]

    @property
    def total_sum_assured(self) -> float:
        """Cover on policies this party owns or is insured under.

        Agent-serviced policies are excluded on purpose. An agent is not exposed
        to the sum assured on a book they sold, and adding it in would put a
        number on the page that is real for nobody.
        """
        return float(sum(
            p.sum_assured_amount or 0
            for p in self.policies
            if p.role_group in ("owns", "insured")
        ))


def load_customer_360(
    conn: psycopg.Connection, person_id: Any, *, policy_limit: int = 500
) -> Customer360 | None:
    """Assemble everything linked to one party. None if there is no such party.

    One function and one connection rather than a query per panel, because the
    panels are read together and a page that issued twelve round trips per view
    would be slow in exactly the deployment this system targets -- a single
    machine with the database on the same disk as everything else.
    """
    from cmdm.store.writer import resolve_person_id

    resolved = resolve_person_id(conn, person_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM mdm.person WHERE person_id = %s AND is_current",
            (resolved,),
        )
        record = cur.fetchone()
        if record is None:
            return None

        policies = _policies(cur, resolved, policy_limit)
        household_id, members, affiliations = _connections(conn, resolved)

        cur.execute(
            """
            SELECT source_system, source_key_kind, source_party_key, is_active,
                   derivation_method, confidence, linked_at
            FROM mdm.person_xref WHERE person_id = %s
            ORDER BY is_active DESC, source_system, source_party_key
            """,
            (resolved,),
        )
        sources = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT attribute_name, strategy, winning_source_system, value_text,
                   candidate_count, rejected_values
            FROM mdm.attribute_provenance
            WHERE entity_id = %s ORDER BY attribute_name
            """,
            (resolved,),
        )
        lineage = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT version, valid_from, valid_to FROM mdm.person "
            "WHERE person_id = %s ORDER BY version DESC LIMIT 20",
            (resolved,),
        )
        versions = [dict(r) for r in cur.fetchall()]

        merged_from, considered = _match_history(cur, resolved)

    return Customer360(
        person_id=str(resolved),
        requested_id=str(person_id),
        record=dict(record),
        policies=policies,
        household_id=household_id,
        household_members=members,
        affiliations=affiliations,
        sources=sources,
        lineage=lineage,
        versions=versions,
        merged_from=merged_from,
        considered=considered,
    )


def _policies(cur: Any, person_id: Any, limit: int) -> list[PolicyRole]:
    """Every policy this party holds a role on, with the other parties on it.

    The counterparties come back in the same pass rather than one query per
    policy. A party on 200 policies would otherwise issue 200 queries to render
    one page, and agents are exactly the parties most worth looking at.
    """
    cur.execute(
        """
        SELECT r.role, p.policy_id::text, p.policy_number, p.source_system,
               p.product_name, p.product_line::text, p.policy_status::text,
               p.currency_code, p.sum_assured_amount, p.annual_premium_amount,
               p.effective_date, p.termination_date
        FROM mdm.relationship r
        JOIN mdm.policy p ON p.policy_id = r.to_policy_id AND p.is_current
        WHERE r.edge_kind = 'PARTY_POLICY' AND r.from_person_id = %s
          AND r.is_current
        ORDER BY p.effective_date DESC NULLS LAST, p.policy_number
        LIMIT %s
        """,
        (person_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return []

    policy_ids = [r["policy_id"] for r in rows]
    cur.execute(
        """
        SELECT r.to_policy_id::text AS policy_id, r.role::text AS role,
               r.from_person_id::text AS person_id, r.stated_relationship,
               pe.full_name, pe.party_type::text
        FROM mdm.relationship r
        JOIN mdm.person pe ON pe.person_id = r.from_person_id AND pe.is_current
        WHERE r.edge_kind = 'PARTY_POLICY'
          AND r.to_policy_id = ANY(%s::uuid[])
          AND r.from_person_id <> %s
          AND r.is_current
        ORDER BY r.role
        """,
        (policy_ids, person_id),
    )
    others: dict[str, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        others.setdefault(row["policy_id"], []).append(dict(row))

    return [
        PolicyRole(
            policy_id=row["policy_id"],
            policy_number=row["policy_number"],
            source_system=row["source_system"],
            product_name=row["product_name"],
            product_line=row["product_line"],
            policy_status=row["policy_status"],
            currency_code=row["currency_code"],
            sum_assured_amount=row["sum_assured_amount"],
            annual_premium_amount=row["annual_premium_amount"],
            effective_date=row["effective_date"],
            termination_date=row["termination_date"],
            role=row["role"],
            role_group=_GROUP_OF.get(row["role"], "other"),
            counterparties=others.get(row["policy_id"], []),
        )
        for row in rows
    ]


def _connections(
    conn: psycopg.Connection, person_id: Any
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Household members and affiliations, each labelled from this party's side.

    Delegates to `household.household_of` rather than re-deriving membership,
    because householding has rules -- stated relationships only, address never
    admitting a member -- and a second implementation of them here would be a
    second place for those rules to drift.
    """
    from cmdm.household import household_of

    found = household_of(conn, person_id)
    named = {str(r["person_id"]): r for r in found["relations"]}

    members = []
    for member in found["members"]:
        if str(member["person_id"]) == str(person_id):
            continue
        relation = named.get(str(member["person_id"]))
        members.append({
            **member,
            "person_id": str(member["person_id"]),
            # A household is transitive, so a member two steps away belongs to
            # it without any edge naming how they relate to *this* party. That
            # member is labelled "same household" rather than given a guessed
            # relationship, which would be an invention presented as a fact.
            "relation": (
                relation_label(relation["association_type"], relation["outgoing"])
                if relation else "same household"
            ),
            "evidence_count": relation["evidence_count"] if relation else 0,
            "stated": relation is not None,
        })

    affiliations = [
        {
            **entry,
            "person_id": str(entry["person_id"]),
            "relation": relation_label(
                entry["association_type"], entry["outgoing"]
            ),
        }
        for entry in found["affiliations"]
    ]

    household_id = found["household_id"]
    return (str(household_id) if household_id else None, members, affiliations)


def _match_history(
    cur: Any, person_id: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What was merged into this party, and what was considered and refused.

    Both halves, because a page that shows only merges answers half the question
    people actually bring to it. "Why are these two not the same person" is
    asked constantly, and the ledger is the only thing that can answer it -- the
    golden record itself has no memory of a party it was decided *not* to be.
    """
    cur.execute(
        """
        SELECT source_system, source_key_kind, source_party_key, derivation_method,
               confidence, linked_at
        FROM mdm.person_xref
        WHERE person_id = %s AND derivation_method <> 'DETERMINISTIC'
        ORDER BY linked_at DESC
        """,
        (person_id,),
    )
    merged_from = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT mp.score, mp.zone::text, mp.final_decision, mp.decided_by,
               mp.ai_score, mp.ai_decision, mp.model_name, mp.created_at,
               mp.left_source_identity, mp.right_source_identity,
               mp.comparator_scores
        FROM mdm.match_pair mp
        WHERE mp.left_person_id = %s OR mp.right_person_id = %s
        ORDER BY mp.score DESC
        LIMIT 25
        """,
        (person_id, person_id),
    )
    considered = []
    for row in cur.fetchall():
        entry = dict(row)
        # The stored identity is `system\x1fkind\x1fkey`; the key alone is what
        # a human recognises, and the separator renders as a control character.
        for side in ("left_source_identity", "right_source_identity"):
            parts = str(entry[side]).split("\x1f")
            entry[side] = parts[-1] if parts else entry[side]
        considered.append(entry)

    return merged_from, considered
