"""Scoring a candidate, and what the score is allowed to decide.

Name agreement alone produces unusable precision on a Philippine book: there
are a great many people called Santos, Reyes and Cruz, and several of them are
on a list somewhere. Corroborating attributes are what make screening
survivable — a date of birth that agrees lifts a hit, one that contradicts by a
decade sinks it, and an identification number that matches ends the discussion.

Two decisions this module makes deliberately:

**Nothing is auto-confirmed.** The strongest score this returns is
``POTENTIAL_MATCH``. Confirming a sanctions match freezes a client's property
and triggers a report to the AMLC, and that decision has a person's name
against it. The system's job is to rank and explain, not to sign.

**A date-of-birth conflict reduces but never eliminates.** Lists carry
approximate and multiple dates of birth, and a designated person is not obliged
to give an insurer their real one. A hard exclusion on a date mismatch is how a
true match gets suppressed by bad data supplied by the very person being
screened.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from aml.config import ScreeningConfig
from aml.model.enums import PartyType, ScreeningDecision
from aml.screening.base import ListEntry, ScreeningHit, Subject
from aml.screening.names import name_similarity

__all__ = ["score_entry", "decide", "screen_candidates"]

_YEAR = re.compile(r"(1[89]\d{2}|20\d{2})")
_ALNUM = re.compile(r"[^A-Z0-9]")


def _normalize_id(value: str) -> str:
    return _ALNUM.sub("", str(value).upper())


def _dob_agreement(subject: Subject, entry: ListEntry) -> tuple[Decimal, str]:
    """Compare a date of birth against however the list expresses one."""
    if subject.birth_date is None or not entry.birth_dates:
        return Decimal("0"), "no date of birth to compare"
    iso = subject.birth_date.isoformat()
    years = {subject.birth_date.year}
    listed_years: set[int] = set()
    for raw in entry.birth_dates:
        text = str(raw).strip()
        if text == iso:
            return Decimal("0.06"), f"date of birth agrees exactly ({iso})"
        listed_years.update(int(m) for m in _YEAR.findall(text))
    if not listed_years:
        return Decimal("0"), "listed date of birth not interpretable"
    if years & listed_years:
        return Decimal("0.03"), f"year of birth agrees ({subject.birth_date.year})"
    nearest = min(abs(subject.birth_date.year - y) for y in listed_years)
    if nearest <= 1:
        return Decimal("0"), "year of birth within one year"
    return (
        Decimal("-0.20"),
        f"year of birth disagrees by {nearest} years "
        f"({subject.birth_date.year} against {sorted(listed_years)})",
    )


def score_entry(subject: Subject, entry: ListEntry) -> ScreeningHit:
    """Score one candidate against one subject."""
    reasons: list[str] = []
    best_score = Decimal("0")
    matched_name = entry.name
    for subject_name in subject.all_names:
        for entry_name in entry.all_names:
            candidate = name_similarity(subject_name, entry_name)
            if candidate > best_score:
                best_score, matched_name = candidate, entry_name
    name_score = best_score
    score = name_score
    reasons.append(f"name agreement {name_score} against {matched_name!r}")

    subject_ids = {_normalize_id(i) for i in subject.id_numbers if i}
    entry_ids = {_normalize_id(i) for i in entry.id_numbers if i}
    shared_ids = {i for i in subject_ids & entry_ids if len(i) >= 5}
    if shared_ids:
        score = max(score, Decimal("0.99"))
        reasons.append(f"identification number matches ({', '.join(sorted(shared_ids))})")
    else:
        adjustment, reason = _dob_agreement(subject, entry)
        score += adjustment
        reasons.append(reason)

        if subject.nationality and subject.nationality.upper() in {
            n.upper() for n in (*entry.nationalities, *entry.countries) if n
        }:
            score += Decimal("0.02")
            reasons.append(f"nationality agrees ({subject.nationality})")

        subject_is_person = subject.party_type is not PartyType.ORGANIZATION
        entry_is_person = (entry.entity_type or "INDIVIDUAL").upper().startswith("IND")
        if subject_is_person != entry_is_person:
            score *= Decimal("0.75")
            reasons.append(
                "entity type differs (a natural person against a legal entity)"
            )

    if entry.provider_score is not None:
        # A vendor asserting a strong match is evidence. Taken as a floor
        # rather than as the answer, and recorded either way.
        if entry.provider_score > score:
            reasons.append(
                f"{entry.provider or 'provider'} scores this {entry.provider_score}, "
                "higher than the local model"
            )
            score = entry.provider_score
        else:
            reasons.append(f"{entry.provider or 'provider'} scores this {entry.provider_score}")

    score = max(Decimal("0"), min(Decimal("1"), score)).quantize(Decimal("0.0001"))
    return ScreeningHit(
        entry=entry,
        name_score=name_score,
        score=score,
        matched_name=matched_name,
        reasons=tuple(reasons),
    )


def decide(score: Decimal, config: ScreeningConfig) -> ScreeningDecision:
    """Turn a score into a triage outcome.

    Note what is missing: there is no branch returning ``TRUE_MATCH``. See the
    module docstring.
    """
    if score >= config.review_threshold:
        return ScreeningDecision.POTENTIAL_MATCH
    return ScreeningDecision.NO_MATCH


def screen_candidates(
    subject: Subject, candidates: Sequence[ListEntry], config: ScreeningConfig
) -> tuple[ScreeningHit, ...]:
    """Score every candidate and keep the ones worth a human's time.

    The sanctions floor is applied here rather than in :func:`decide`, because
    it depends on which list the candidate is on. A near-exact name against a
    designated person is raised even when the corroborating attributes pull the
    score below the review threshold: the attributes came from the client, and
    a client who gives a false date of birth would otherwise be screening
    themselves out.
    """
    scored = []
    for entry in candidates:
        hit = score_entry(subject, entry)
        decision = decide(hit.score, config)
        reasons = hit.reasons
        if (
            decision is ScreeningDecision.NO_MATCH
            and entry.is_sanctions
            and hit.name_score >= config.sanctions_name_floor
        ):
            decision = ScreeningDecision.POTENTIAL_MATCH
            reasons = reasons + (
                f"retained below the {config.review_threshold} review threshold: name "
                f"agreement {hit.name_score} against a designated-person list reaches the "
                f"{config.sanctions_name_floor} name floor",
            )
        if decision is ScreeningDecision.NO_MATCH:
            continue
        scored.append(
            ScreeningHit(
                entry=hit.entry,
                name_score=hit.name_score,
                score=hit.score,
                matched_name=hit.matched_name,
                reasons=reasons,
                decision=decision,
            )
        )
    scored.sort(key=lambda h: (-h.score, h.entry.entry_id))
    return tuple(scored)
