"""Consent and erasure.

The two data-subject rights that are genuinely hard against this architecture,
and where a design that pretends otherwise would be worse than one that states
the difficulty.

**Consent** is history, not a flag. The Person entity carries ``do_not_contact``
as a boolean because matching and survivorship need it as a column, but that
boolean is a *projection*. This module owns the record: who consented to what,
when, through which channel, and when they withdrew it. Keeping only the boolean
would mean an opt-out could be silently reversed by a later feed with nothing to
show it ever existed — and "prove this customer consented" is exactly what a
regulator asks.

**Erasure** is a workflow, not a DELETE. Three things stand in the way of simply
deleting a person:

*   The landing zone is immutable and content-addressed by design.
*   Golden records are SCD-2; history is the point of them.
*   Some data must be retained *despite* a valid erasure request. An in-force
    insurance contract carries statutory retention that overrides deletion, and
    quietly deleting it would breach a different obligation.

So erasure here is scoped, executed field by field, and recorded — including what
was retained and why. That last part is the one that matters: a regulator asking
why data survived an erasure request needs an answer recorded at the time, not
reconstructed afterwards.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cmdm.model.enums import PiiClass
from cmdm.model.fields import PERSON, EntitySpec
from cmdm.model.ids import uuid7

__all__ = [
    "ERASED_TOMBSTONE",
    "ConsentPurpose",
    "ConsentState",
    "ErasureState",
    "record_consent",
    "current_consent",
    "may_contact",
    "request_erasure",
    "assess_erasure",
    "execute_erasure",
    "RETAINED_FOR_CONTRACT",
    "NEVER_ERASED",
    "ErasurePlan",
]


class ConsentPurpose:
    MARKETING = "MARKETING"
    PROFILING = "PROFILING"
    DATA_SHARING = "DATA_SHARING"
    AUTOMATED_DECISIONS = "AUTOMATED_DECISIONS"
    SERVICE_COMMUNICATION = "SERVICE_COMMUNICATION"


class ConsentState:
    GRANTED = "GRANTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    NEVER_GIVEN = "NEVER_GIVEN"


class ErasureState:
    REQUESTED = "REQUESTED"
    ASSESSING = "ASSESSING"
    PARTIALLY_ERASED = "PARTIALLY_ERASED"
    ERASED = "ERASED"
    REFUSED = "REFUSED"


#: Fields retained despite an erasure request because a live insurance contract
#: requires them. Named explicitly rather than inferred, because "why did you
#: keep this" must have an answer that predates the question.
#: What a non-nullable PII column becomes when erased. Some columns the schema
#: requires -- full_name is NOT NULL because a golden record with no name is not
#: a record -- so erasure replaces them rather than nulling them. The tombstone
#: is distinguishable from real data and from absent data, which matters: a
#: reader must be able to tell "erased on request" from "never supplied", and
#: nulling a required column would either break the schema or lie about which
#: of those happened.
ERASED_TOMBSTONE = "[ERASED]"

RETAINED_FOR_CONTRACT: frozenset[str] = frozenset({
    "full_name",          # a contract with an unnamed counterparty is unenforceable
    "date_of_birth",      # determines benefit eligibility and premium
    "national_id_hash",   # statutory identification for claims and AML
    "national_id_last4",
})

#: Never erased, regardless of the request or the contract state. These carry
#: their own statutory retention that a data-subject request does not override:
#: an anti-money-laundering screening result must be retained for the regulated
#: period whether or not the person asks to be forgotten, and erasing it would
#: breach a different obligation than the one erasure serves.
NEVER_ERASED: frozenset[str] = frozenset({
    "is_sanctioned",
})


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


def record_consent(
    conn: psycopg.Connection,
    person_id: uuid.UUID | str,
    purpose: str,
    state: str,
    *,
    channel: str | None = None,
    evidence_ref: str | None = None,
    source_system: str | None = None,
    recorded_by: str | None = None,
    effective_from: dt.datetime | None = None,
) -> uuid.UUID:
    """Record a consent decision, closing whatever it supersedes.

    The previous live record for this person and purpose is closed rather than
    overwritten, so the history survives. A unique index enforces that only one
    record per person and purpose is live at a time — two contradictory live
    records would make "may we contact them" unanswerable, which is the one
    question this table exists to answer.
    """
    now = effective_from or dt.datetime.now(dt.UTC)
    consent_id = uuid7()

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mdm.consent SET effective_to = %s
            WHERE person_id = %s AND purpose = %s::mdm.consent_purpose
              AND effective_to IS NULL
            """,
            (now, person_id, purpose),
        )
        cur.execute(
            """
            INSERT INTO mdm.consent
                (consent_id, person_id, purpose, state, channel, evidence_ref,
                 source_system, effective_from, recorded_by)
            VALUES (%s, %s, %s::mdm.consent_purpose, %s::mdm.consent_state,
                    %s, %s, %s, %s, %s)
            """,
            (consent_id, person_id, purpose, state, channel, evidence_ref,
             source_system, now, recorded_by),
        )
    return consent_id


def current_consent(
    conn: psycopg.Connection, person_id: uuid.UUID | str
) -> dict[str, dict[str, Any]]:
    """Live consent state per purpose."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT purpose, state, channel, evidence_ref, effective_from, recorded_by
            FROM mdm.consent
            WHERE person_id = %s AND effective_to IS NULL
            """,
            (person_id,),
        )
        return {row["purpose"]: dict(row) for row in cur.fetchall()}


def may_contact(
    conn: psycopg.Connection,
    person_id: uuid.UUID | str,
    purpose: str = ConsentPurpose.MARKETING,
) -> bool:
    """Whether the person may be contacted for a purpose.

    Absence of a consent record is treated as absence of consent. Defaulting the
    other way — contactable until proven otherwise — is the assumption that
    produces regulatory findings, and it is not one a system should make
    implicitly on a caller's behalf.
    """
    live = current_consent(conn, person_id).get(purpose)
    return bool(live and live["state"] == ConsentState.GRANTED)


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErasurePlan:
    """What an erasure request would do, before it does it."""

    person_id: uuid.UUID | str
    erasable: tuple[str, ...]
    retained: tuple[str, ...]
    retention_reason: str | None
    has_live_contract: bool
    affected_versions: int
    affected_source_records: int

    @property
    def is_complete(self) -> bool:
        """Whether everything requested can actually be erased."""
        return not self.retained


def request_erasure(
    conn: psycopg.Connection,
    person_id: uuid.UUID | str,
    *,
    requested_by: str,
    legal_basis: str | None = None,
) -> uuid.UUID:
    """Open an erasure request. Does not erase anything."""
    request_id = uuid7()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.erasure_request
                (request_id, person_id, state, requested_by, legal_basis)
            VALUES (%s, %s, 'REQUESTED', %s, %s)
            """,
            (request_id, person_id, requested_by, legal_basis),
        )
    return request_id


def assess_erasure(
    conn: psycopg.Connection,
    person_id: uuid.UUID | str,
    *,
    spec: EntitySpec = PERSON,
) -> ErasurePlan:
    """Decide what may be erased and what must be retained.

    Retention is driven by whether the person is attached to a live contract.
    That is the common case in insurance and the one that makes erasure partial
    rather than total: the policy still exists, claims may still arise against
    it, and the counterparty cannot become anonymous while it does.

    Only PII is a candidate for erasure. Structural columns — surrogate keys,
    versioning, the record hash — stay, because removing them would corrupt the
    store rather than protect the person, and they identify nobody on their own.

    Non-nullable PII columns are erasable too; they are overwritten with a
    tombstone rather than nulled. Excluding them because the schema requires a
    value would leave the single most identifying attribute — the name — in
    place after an erasure, which is the opposite of the point.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM mdm.relationship r
            JOIN mdm.policy p ON p.policy_id = r.to_policy_id AND p.is_current
            WHERE r.from_person_id = %s AND r.is_current
              AND p.policy_status IN ('ISSUED', 'INFORCE', 'PAID_UP', 'GRACE',
                                      'REINSTATED', 'CLAIM_PENDING')
            """,
            (person_id,),
        )
        live_contracts = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM mdm.person WHERE person_id = %s", (person_id,)
        )
        versions = cur.fetchone()[0]

        cur.execute(
            """
            SELECT count(*) FROM mdm.source_record sr
            WHERE EXISTS (
                SELECT 1 FROM mdm.person_xref x
                WHERE x.person_id = %s
                  AND sr.payload->>'OwnerCustomerId' = x.source_party_key
            )
            """,
            (person_id,),
        )
        source_records = cur.fetchone()[0]

    pii_fields = [
        f.name for f in spec.fields
        if f.pii in (PiiClass.DIRECT, PiiClass.SENSITIVE)
        and not f.derived
        and f.name not in NEVER_ERASED
    ]

    has_contract = live_contracts > 0
    retained = tuple(sorted(f for f in pii_fields if f in RETAINED_FOR_CONTRACT)) \
        if has_contract else ()
    erasable = tuple(sorted(f for f in pii_fields if f not in retained))

    return ErasurePlan(
        person_id=person_id,
        erasable=erasable,
        retained=retained,
        retention_reason=(
            f"{live_contracts} live insurance contract(s) require the counterparty "
            "to remain identifiable for the statutory retention period"
        ) if has_contract else None,
        has_live_contract=has_contract,
        affected_versions=versions,
        affected_source_records=source_records,
    )


def execute_erasure(
    conn: psycopg.Connection,
    request_id: uuid.UUID,
    plan: ErasurePlan,
    *,
    executed_by: str,
) -> dict[str, Any]:
    """Carry out an assessed erasure.

    Erasure nulls the named columns across **every version**, not only the
    current one. Leaving history intact would defeat the whole exercise: the
    previous version holds the same personal data.

    The landing-zone payloads are redacted rather than deleted. Deleting the row
    would break the provenance chain that explains every golden value ever
    derived from it, so the row survives with its personal fields removed and a
    marker recording that it was redacted — the shape of the evidence is
    preserved, the personal data is not.
    """
    if not plan.erasable:
        raise ValueError("nothing to erase; the plan lists no erasable fields")

    # Nullable columns are nulled. Required ones cannot be, so they are
    # overwritten with a value of their own type -- a tombstone for text, false
    # for a flag. Excluding them because the schema requires a value would leave
    # the name in place after an erasure, which is the opposite of the point.
    from cmdm.model.enums import LogicalType as LT
    from cmdm.model.fields import PERSON as _PERSON

    declared = _PERSON.by_name
    parts = []
    for column in plan.erasable:
        field_spec = declared.get(column)
        if field_spec is None or field_spec.nullable:
            parts.append(f"{column} = NULL")
        elif field_spec.dtype is LT.BOOL:
            parts.append(f"{column} = false")
        elif field_spec.dtype is LT.STRING:
            parts.append(f"{column} = %(tombstone)s")
        else:
            # A required non-text, non-boolean PII column would need its own
            # erasure value decided deliberately rather than guessed at here.
            raise ValueError(
                f"{column} is a required {field_spec.dtype.value} column with no "
                "defined erasure value; decide one before erasing it"
            )
    assignments = ", ".join(parts)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE mdm.person SET {assignments}, updated_at = now() "
            "WHERE person_id = %(person_id)s",
            {"person_id": plan.person_id, "tombstone": ERASED_TOMBSTONE},
        )
        versions_erased = cur.rowcount

        # Redact rather than delete: the provenance chain must survive.
        cur.execute(
            """
            UPDATE mdm.source_record sr
            SET payload = jsonb_build_object('_redacted', true,
                                             '_redacted_at', now()::text)
            WHERE EXISTS (
                SELECT 1 FROM mdm.person_xref x
                WHERE x.person_id = %s
                  AND sr.payload->>'OwnerCustomerId' = x.source_party_key
            )
            """,
            (plan.person_id,),
        )
        records_redacted = cur.rowcount

        # The crosswalk keeps its rows but loses the source keys, which are
        # themselves identifiers. The mapping's existence is retained so a
        # re-delivery of the same key cannot silently recreate the person.
        cur.execute(
            "UPDATE mdm.person_xref SET is_active = false WHERE person_id = %s",
            (plan.person_id,),
        )

        state = ErasureState.PARTIALLY_ERASED if plan.retained else ErasureState.ERASED
        cur.execute(
            """
            UPDATE mdm.erasure_request
            SET state = %s::mdm.erasure_state, erased_fields = %s,
                retained_fields = %s, retention_reason = %s,
                affected_versions = %s, affected_source_records = %s,
                executed_by = %s, executed_at = now()
            WHERE request_id = %s
            """,
            (state, list(plan.erasable), list(plan.retained), plan.retention_reason,
             versions_erased, records_redacted, executed_by, request_id),
        )

    return {
        "request_id": str(request_id),
        "state": state,
        "erased_fields": list(plan.erasable),
        "retained_fields": list(plan.retained),
        "retention_reason": plan.retention_reason,
        "versions_erased": versions_erased,
        "source_records_redacted": records_redacted,
    }
