"""What a screening provider is asked, and what it returns.

The protocol is deliberately thin: a provider *finds candidates*, it does not
decide. Deciding happens in one place — :mod:`aml.screening.matcher` — for a
reason that matters in an examination: World-Check, Dow Jones and an internal
list all score differently, and if each provider's own number went straight to
the analyst then the meaning of "0.85" would depend on which vendor the
institution had a contract with that year. Candidates in, one scoring model,
one threshold, one audit trail.

A vendor's own score is not thrown away. It is carried on the entry and taken
into account, because a provider asserting a strong match is evidence — just
not the only evidence, and not evidence the institution can explain to a
regulator on its own.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from aml.model.entities import Party, jsonable
from aml.model.enums import ListType, PartyType, ScreeningDecision

__all__ = ["Subject", "ListEntry", "ScreeningHit", "ScreeningResult", "ScreeningProvider"]


@dataclass(frozen=True, slots=True)
class Subject:
    """The person or entity being screened."""

    subject_id: str
    name: str
    party_type: PartyType = PartyType.INDIVIDUAL
    aliases: tuple[str, ...] = ()
    birth_date: dt.date | None = None
    nationality: str = ""
    country: str = ""
    id_numbers: tuple[str, ...] = ()
    #: What produced this subject: a customer, a counterparty, a beneficiary.
    role: str = "customer"

    @classmethod
    def from_party(cls, party: Party, role: str = "customer") -> Subject:
        return cls(
            subject_id=party.party_id,
            name=party.display_name,
            party_type=party.party_type,
            aliases=tuple(party.aliases),
            birth_date=party.birth_date,
            nationality=party.nationality,
            country=party.country_of_residence or party.country,
            id_numbers=tuple(doc.number for doc in party.identifications if doc.number),
            role=role,
        )

    @property
    def all_names(self) -> tuple[str, ...]:
        return tuple(n for n in (self.name, *self.aliases) if n)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "name": self.name,
            "party_type": str(self.party_type),
            "aliases": list(self.aliases),
            "birth_date": jsonable(self.birth_date),
            "nationality": self.nationality,
            "country": self.country,
            "id_numbers": list(self.id_numbers),
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class ListEntry:
    """One record on a watch list, from whichever provider holds it."""

    entry_id: str
    name: str
    list_type: ListType = ListType.OTHER
    list_name: str = ""
    provider: str = ""
    aliases: tuple[str, ...] = ()
    entity_type: str = "INDIVIDUAL"
    #: Dates as published: often a year alone, sometimes several candidates,
    #: sometimes a range. Kept as strings because that is what they are.
    birth_dates: tuple[str, ...] = ()
    nationalities: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    id_numbers: tuple[str, ...] = ()
    #: Sanctions programmes, or the office held by a PEP.
    designations: tuple[str, ...] = ()
    positions: tuple[str, ...] = ()
    remarks: str = ""
    listed_on: str = ""
    #: The provider's own confidence, where it supplies one.
    provider_score: Decimal | None = None
    source_reference: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def all_names(self) -> tuple[str, ...]:
        return tuple(n for n in (self.name, *self.aliases) if n)

    @property
    def is_sanctions(self) -> bool:
        return self.list_type in (
            ListType.SANCTIONS,
            ListType.UN_DESIGNATED,
            ListType.ATC_DESIGNATED,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ListEntry:
        """Rebuild an entry from its stored form (used by the cache)."""
        score = raw.get("provider_score")
        return cls(
            entry_id=str(raw.get("entry_id", "")),
            name=str(raw.get("name", "")),
            list_type=ListType(str(raw.get("list_type", "OTHER"))),
            list_name=str(raw.get("list_name", "")),
            provider=str(raw.get("provider", "")),
            aliases=tuple(raw.get("aliases", ()) or ()),
            entity_type=str(raw.get("entity_type", "INDIVIDUAL")),
            birth_dates=tuple(raw.get("birth_dates", ()) or ()),
            nationalities=tuple(raw.get("nationalities", ()) or ()),
            countries=tuple(raw.get("countries", ()) or ()),
            id_numbers=tuple(raw.get("id_numbers", ()) or ()),
            designations=tuple(raw.get("designations", ()) or ()),
            positions=tuple(raw.get("positions", ()) or ()),
            remarks=str(raw.get("remarks", "")),
            listed_on=str(raw.get("listed_on", "")),
            provider_score=None if score in (None, "") else Decimal(str(score)),
            source_reference=str(raw.get("source_reference", "")),
            raw=dict(raw.get("raw", {}) or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "list_type": str(self.list_type),
            "list_name": self.list_name,
            "provider": self.provider,
            "aliases": list(self.aliases),
            "entity_type": self.entity_type,
            "birth_dates": list(self.birth_dates),
            "nationalities": list(self.nationalities),
            "countries": list(self.countries),
            "id_numbers": list(self.id_numbers),
            "designations": list(self.designations),
            "positions": list(self.positions),
            "remarks": self.remarks,
            "listed_on": self.listed_on,
            "provider_score": None if self.provider_score is None else str(self.provider_score),
            "source_reference": self.source_reference,
        }


@dataclass(frozen=True, slots=True)
class ScreeningHit:
    """One candidate, scored, with the reasoning attached.

    ``reasons`` is not decoration. When an analyst clears a hit as a false
    positive they are overruling this score, and the reasons are what they are
    overruling — "names agree at 0.94, dates of birth disagree by 11 years" is
    a decision somebody can review. A bare number is not.
    """

    entry: ListEntry
    name_score: Decimal
    score: Decimal
    matched_name: str
    reasons: tuple[str, ...] = ()
    decision: ScreeningDecision = ScreeningDecision.PENDING

    @property
    def is_strong(self) -> bool:
        return self.score >= Decimal("0.93")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.as_dict(),
            "name_score": str(self.name_score),
            "score": str(self.score),
            "matched_name": self.matched_name,
            "reasons": list(self.reasons),
            "decision": str(self.decision),
        }


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Everything one screening of one subject produced.

    Recorded whether or not anything was found. "We screened this client on
    this date against these lists and found nothing" is precisely the assertion
    an examiner asks a covered person to evidence, and it cannot be
    reconstructed later from an empty table.
    """

    subject: Subject
    screened_at: dt.datetime
    providers: tuple[str, ...]
    hits: tuple[ScreeningHit, ...] = ()
    lists_searched: tuple[str, ...] = ()
    from_cache: bool = False
    error: str = ""

    @property
    def decision(self) -> ScreeningDecision:
        if self.error:
            return ScreeningDecision.PENDING
        if not self.hits:
            return ScreeningDecision.NO_MATCH
        return max((h.decision for h in self.hits), key=_decision_rank)

    @property
    def best(self) -> ScreeningHit | None:
        return max(self.hits, key=lambda h: h.score, default=None)

    @property
    def sanctions_hits(self) -> tuple[ScreeningHit, ...]:
        return tuple(h for h in self.hits if h.entry.is_sanctions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject.as_dict(),
            "screened_at": self.screened_at.isoformat(),
            "providers": list(self.providers),
            "lists_searched": list(self.lists_searched),
            "decision": str(self.decision),
            "hit_count": len(self.hits),
            "hits": [h.as_dict() for h in self.hits],
            "from_cache": self.from_cache,
            "error": self.error,
        }


_DECISION_ORDER = {
    ScreeningDecision.NO_MATCH: 0,
    ScreeningDecision.FALSE_POSITIVE: 1,
    ScreeningDecision.PENDING: 2,
    ScreeningDecision.POTENTIAL_MATCH: 3,
    ScreeningDecision.TRUE_MATCH: 4,
}


def _decision_rank(decision: ScreeningDecision) -> int:
    return _DECISION_ORDER.get(decision, 0)


@runtime_checkable
class ScreeningProvider(Protocol):
    """A source of candidate list entries."""

    name: str

    def candidates(self, subject: Subject, limit: int = 25) -> Sequence[ListEntry]:
        """Return plausible matches for the subject. Scoring happens elsewhere."""
        ...

    def lists(self) -> Sequence[str]:
        """Names of the lists this provider searched, for the audit record."""
        ...
