"""The filing workflow: determination, approval, deadlines, narrative.

Four things happen here, and each exists because of a specific way filings go
wrong in practice.

**Determination is an event with a timestamp and an author.** The five-working-
day clock runs from the moment the institution determined a transaction to be
suspicious — not from the alert, not from the report. Recording it as a
distinct act is what makes lateness measurable, and what stops an institution
from discovering at examination that it cannot say when it knew.

**Approval is separated from determination.** The analyst who investigates does
not approve the filing; the compliance officer does. :func:`approve` refuses
when they are the same person. This is not ceremony — an STR is a serious
assertion about a client, and a single pair of eyes is how both false filings
and quiet non-filings happen.

**Deadlines are tracked in working days, on the Philippine calendar**, with a
separate and much shorter clock for terrorism-financing and designated-person
matters, where the obligation is to freeze without delay and report in hours.

**The narrative is composed, not left blank.** An STR whose narrative says
"suspicious activity detected" tells the AMLC nothing. The composer assembles
what the system already knows — who the client is, what they did, over what
period, which circumstances are relied on, what the institution did about it —
into a draft the analyst edits rather than a blank box they dread.

One thing this module deliberately does not do is notify anyone outside the
compliance function. Disclosing that a report is being filed is prohibited, and
a workflow that emails a relationship manager "case opened on your client" is
a tipping-off incident waiting to happen.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from aml.case.store import AmlStore, WorkflowError
from aml.config import AmlConfig
from aml.model.codes import SUSPICIOUS_CIRCUMSTANCES, UNLAWFUL_ACTIVITIES
from aml.model.entities import Alert, Book, Case
from aml.model.enums import AlertState, CaseState, ReportKind, Severity
from aml.money import PHP, Money
from aml.phcalendar import now_manila

__all__ = [
    "DeadlineStatus",
    "determine",
    "approve",
    "mark_filed",
    "close_case",
    "compose_narrative",
    "deadline_status",
    "due_soon",
]

#: Codes and rules that put a case on the terrorism-financing clock.
_TF_CODES = frozenset({"UA13", "UA14"})
_TF_RULES = frozenset({"str.designated_party", "screening.sanctions_match"})


@dataclass(frozen=True, slots=True)
class DeadlineStatus:
    """Where a case stands against its filing deadline."""

    case_id: str
    kind: ReportKind
    subject_party_id: str
    state: CaseState
    due_on: dt.date | None
    working_days_remaining: int | None
    overdue: bool
    clock: str
    at_risk: bool
    determined_at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": str(self.kind),
            "subject_party_id": self.subject_party_id,
            "state": str(self.state),
            "due_on": self.due_on.isoformat() if self.due_on else None,
            "working_days_remaining": self.working_days_remaining,
            "overdue": self.overdue,
            "clock": self.clock,
            "at_risk": self.at_risk,
        }


def _is_terrorism_related(case: Case, alerts: Sequence[Alert]) -> bool:
    if _TF_CODES & set(case.unlawful_activity_codes):
        return True
    return any(a.rule_id in _TF_RULES for a in alerts)


def determine(
    store: AmlStore,
    case_id: str,
    *,
    actor: str,
    config: AmlConfig,
    narrative: str = "",
    st_codes: Sequence[str] = (),
    unlawful_activity_codes: Sequence[str] = (),
    at: dt.datetime | None = None,
) -> Case:
    """Record that the institution has determined the case reportable.

    Sets the filing deadline from the determination, on the right clock. An STR
    with no suspicious circumstance named is refused here rather than at the
    portal: the circumstance is the report's whole legal basis.
    """
    case = store.get_case(case_id)
    if case is None:
        raise WorkflowError(f"no case {case_id}")
    if case.state in (CaseState.FILED, CaseState.CLOSED_NOT_REPORTABLE):
        raise WorkflowError(f"case {case_id} is {case.state} and cannot be determined again")

    moment = at or now_manila()
    codes = tuple(dict.fromkeys([*case.st_codes, *st_codes]))
    if case.kind is ReportKind.STR and not codes:
        raise WorkflowError(
            f"case {case_id} names no suspicious circumstance; an STR must cite at least "
            "one of ST1-ST7"
        )
    unknown = [c for c in codes if c not in SUSPICIOUS_CIRCUMSTANCES]
    if unknown:
        raise WorkflowError(f"unknown suspicious circumstance code(s): {', '.join(unknown)}")
    ua_codes = tuple(dict.fromkeys([*case.unlawful_activity_codes, *unlawful_activity_codes]))
    unknown_ua = [c for c in ua_codes if c not in UNLAWFUL_ACTIVITIES]
    if unknown_ua:
        raise WorkflowError(f"unknown unlawful activity code(s): {', '.join(unknown_ua)}")

    alerts = store.alerts(case_id=case_id)
    terrorism = _is_terrorism_related(case, alerts)
    if terrorism:
        # Hours, not working days. The deadline lands on the calendar day the
        # 24 hours expire, and the case is flagged so it sorts to the top.
        deadline = (moment + dt.timedelta(hours=config.deadlines.tf_report_hours)).date()
    else:
        deadline = config.calendar.add_working_days(
            moment.date(),
            config.deadlines.ctr_working_days
            if case.kind is ReportKind.CTR
            else config.deadlines.str_working_days,
        )

    updated = replace(
        case,
        state=CaseState.PENDING_APPROVAL,
        determined_at=moment,
        determined_by=actor,
        filing_deadline=deadline,
        st_codes=codes,
        unlawful_activity_codes=ua_codes,
        narrative=narrative or case.narrative,
        priority=Severity.CRITICAL if terrorism else case.priority,
    )
    return store.update_case(
        updated,
        actor=actor,
        action="case.determined",
        detail={
            "determined_at": moment.isoformat(),
            "filing_deadline": deadline.isoformat(),
            "clock": "terrorism_financing" if terrorism else "working_days",
            "st_codes": list(codes),
            "unlawful_activity_codes": list(ua_codes),
        },
    )


def approve(
    store: AmlStore,
    case_id: str,
    *,
    approver: str,
    note: str = "",
    at: dt.datetime | None = None,
    allow_self_approval: bool = False,
) -> Case:
    """Approve a determined case for filing. Two people, not one."""
    case = store.get_case(case_id)
    if case is None:
        raise WorkflowError(f"no case {case_id}")
    if case.state is not CaseState.PENDING_APPROVAL:
        raise WorkflowError(
            f"case {case_id} is {case.state}; only a case pending approval can be approved"
        )
    if not allow_self_approval and approver.strip().lower() == case.determined_by.strip().lower():
        raise WorkflowError(
            f"{approver} determined this case and cannot also approve it; "
            "filing requires a second person"
        )
    moment = at or now_manila()
    updated = replace(
        case, state=CaseState.APPROVED_FOR_FILING, approved_by=approver, approved_at=moment
    )
    return store.update_case(
        updated,
        actor=approver,
        action="case.approved",
        detail={"note": note, "approved_at": moment.isoformat()},
    )


def mark_filed(
    store: AmlStore,
    case_id: str,
    *,
    actor: str,
    reference: str = "",
    at: dt.datetime | None = None,
) -> Case:
    """Record that the report has actually reached the AMLC."""
    case = store.get_case(case_id)
    if case is None:
        raise WorkflowError(f"no case {case_id}")
    if case.state is not CaseState.APPROVED_FOR_FILING:
        raise WorkflowError(
            f"case {case_id} is {case.state}; it must be approved before it can be filed"
        )
    moment = at or now_manila()
    late = bool(case.filing_deadline and moment.date() > case.filing_deadline)
    updated = replace(
        case,
        state=CaseState.FILED,
        filed_at=moment,
        reference_number=reference or case.reference_number,
    )
    store.update_case(
        updated,
        actor=actor,
        action="case.filed",
        detail={
            "filed_at": moment.isoformat(),
            "reference": reference,
            # Recorded rather than hidden. A late filing is a fact the
            # institution has to report on and explain, and a system that
            # quietly normalises it is doing the opposite of its job.
            "late": late,
            "deadline": case.filing_deadline.isoformat() if case.filing_deadline else None,
        },
    )
    for alert in store.alerts(case_id=case_id):
        if alert.state is not AlertState.CLOSED_REPORTED:
            store.set_alert_state(
                alert.alert_id,
                AlertState.CLOSED_REPORTED,
                actor=actor,
                reason=f"reported under case {case_id}",
            )
    return updated


def close_case(
    store: AmlStore, case_id: str, *, actor: str, reason: str, at: dt.datetime | None = None
) -> Case:
    """Close a case as not reportable. A reason is mandatory."""
    if not reason.strip():
        raise WorkflowError("closing a case without filing requires a written reason")
    case = store.get_case(case_id)
    if case is None:
        raise WorkflowError(f"no case {case_id}")
    if case.state is CaseState.FILED:
        raise WorkflowError(f"case {case_id} has been filed and cannot be closed as unreportable")
    updated = replace(case, state=CaseState.CLOSED_NOT_REPORTABLE, closure_reason=reason)
    store.update_case(
        updated,
        actor=actor,
        action="case.closed_not_reportable",
        detail={"reason": reason, "at": (at or now_manila()).isoformat()},
    )
    for alert in store.alerts(case_id=case_id):
        store.set_alert_state(
            alert.alert_id,
            AlertState.CLOSED_NOT_SUSPICIOUS,
            actor=actor,
            reason=f"case {case_id} closed: {reason}",
        )
    return updated


def deadline_status(
    case: Case, config: AmlConfig, *, as_of: dt.date | None = None, terrorism: bool = False
) -> DeadlineStatus:
    """How long is left, in working days."""
    today = as_of or now_manila().date()
    if case.filing_deadline is None:
        return DeadlineStatus(
            case_id=case.case_id,
            kind=case.kind,
            subject_party_id=case.subject_party_id,
            state=case.state,
            due_on=None,
            working_days_remaining=None,
            overdue=False,
            clock="not started",
            at_risk=False,
            determined_at=case.determined_at,
        )
    remaining = config.calendar.working_days_between(today, case.filing_deadline)
    overdue = case.filing_deadline < today and case.state not in (
        CaseState.FILED,
        CaseState.CLOSED_NOT_REPORTABLE,
    )
    return DeadlineStatus(
        case_id=case.case_id,
        kind=case.kind,
        subject_party_id=case.subject_party_id,
        state=case.state,
        due_on=case.filing_deadline,
        working_days_remaining=remaining,
        overdue=overdue,
        clock="terrorism_financing" if terrorism else "working_days",
        at_risk=(
            not overdue
            and remaining <= config.deadlines.escalate_when_days_remaining
            and case.state not in (CaseState.FILED, CaseState.CLOSED_NOT_REPORTABLE)
        ),
        determined_at=case.determined_at,
    )


def due_soon(
    store: AmlStore, config: AmlConfig, *, as_of: dt.date | None = None
) -> list[DeadlineStatus]:
    """Every live case with a deadline, worst first."""
    statuses = [
        deadline_status(case, config, as_of=as_of)
        for case in store.cases()
        if case.state not in (CaseState.FILED, CaseState.CLOSED_NOT_REPORTABLE)
    ]
    statuses.sort(
        key=lambda s: (
            not s.overdue,
            s.working_days_remaining if s.working_days_remaining is not None else 9999,
        )
    )
    return statuses


def compose_narrative(
    case: Case, alerts: Sequence[Alert], book: Book, config: AmlConfig
) -> str:
    """Draft the STR narrative from what the system already knows.

    Structured the way an AMLC reader needs it: who, what, how much, over what
    period, why it is suspicious, and what the institution has done. The
    analyst edits this; they should not have to start from nothing.
    """
    party = book.party(case.subject_party_id)
    name = party.display_name if party else case.subject_party_id
    lines: list[str] = []

    # 1. The subject.
    subject_bits = [f"{name} ({case.subject_party_id})"]
    if party is not None:
        if party.birth_date:
            subject_bits.append(f"born {party.birth_date.strftime('%d %B %Y')}")
        if party.occupation:
            subject_bits.append(party.occupation.lower())
        if party.customer_since:
            subject_bits.append(f"a client since {party.customer_since.strftime('%B %Y')}")
        if party.is_pep:
            subject_bits.append(
                f"recorded as a {str(party.pep_status).replace('_', ' ').lower()}"
            )
        if party.declared_income:
            subject_bits.append(f"declared income {party.declared_income.quantized()}")
    lines.append("SUBJECT. " + ", ".join(subject_bits) + ".")

    # 2. The activity.
    transactions = [
        t for t in (book.transaction(i) for i in case.transaction_ids) if t is not None
    ]
    monetary = [t for t in transactions if t.is_monetary]
    if monetary:
        total = Money.zero(PHP)
        for txn in monetary:
            total = total + txn.php
        first = min(t.transaction_date for t in monetary)
        last = max(t.transaction_date for t in monetary)
        instruments = sorted({str(t.instrument).replace("_", " ").lower() for t in monetary})
        policies = [book.policy(p) for p in case.policy_ids]
        policy_numbers = ", ".join(p.policy_number or p.policy_id for p in policies if p)
        lines.append(
            f"ACTIVITY. {len(transactions)} transactions totalling {total.quantized()} between "
            f"{first.strftime('%d %B %Y')} and {last.strftime('%d %B %Y')}, settled by "
            f"{', '.join(instruments)}"
            + (f", on {policy_numbers}" if policy_numbers else "")
            + "."
        )

    # 3. Why it is suspicious — the rules' own narratives, which were written
    #    for exactly this.
    if alerts:
        lines.append("GROUNDS FOR SUSPICION.")
        for index, alert in enumerate(sorted(alerts, key=lambda a: -a.score), start=1):
            lines.append(f"  {index}. {alert.narrative}")

    # 4. The statutory circumstances relied on.
    if case.st_codes:
        cited = [
            f"{code} ({SUSPICIOUS_CIRCUMSTANCES[code].label.lower()})"
            for code in case.st_codes
            if code in SUSPICIOUS_CIRCUMSTANCES
        ]
        lines.append("SUSPICIOUS CIRCUMSTANCES RELIED ON. " + "; ".join(cited) + ".")
    if case.unlawful_activity_codes:
        offences = [
            f"{code} ({UNLAWFUL_ACTIVITIES[code].label})"
            for code in case.unlawful_activity_codes
            if code in UNLAWFUL_ACTIVITIES
        ]
        lines.append("SUSPECTED UNLAWFUL ACTIVITY. " + "; ".join(offences) + ".")

    # 5. What was done.
    actions = []
    if party is not None and party.is_frozen:
        actions.append(
            "the property has been frozen and the AMLC informed as required for a "
            "designated person"
        )
    actions.append("the relationship remains under enhanced monitoring")
    actions.append(
        "no disclosure of this report has been made to the client or to any person other "
        "than those required to act on it"
    )
    taken = "; ".join(actions)
    lines.append("ACTION TAKEN BY THE COVERED PERSON. " + taken[:1].upper() + taken[1:] + ".")

    return "\n".join(lines)
