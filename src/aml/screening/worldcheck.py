"""World-Check One (Refinitiv/LSEG) as a candidate source.

The gateway authenticates each request with an HMAC signature over a
canonicalised set of headers rather than with a bearer token, which is why this
module carries its own signing code:

    (request-target): post /v2/cases/screeningRequest
    host: api-worldcheck.refinitiv.com
    date: Tue, 08 Sep 2026 12:00:00 GMT
    content-type: application/json
    content-length: 214

signed with HMAC-SHA256 under the API secret and presented as an
``Authorization: Signature`` header. GET requests sign only the first three
lines, because there is no body to bind. The signature covers the date, so a
clock more than a few minutes out of true is rejected as replay — a failure
that reads like bad credentials and is not.

**On field mapping.** The request payload and the response shape vary by tenant
configuration and by API version, and this module cannot verify yours from
here. Everything version-specific is confined to :data:`STRENGTH_SCORES`,
:data:`CATEGORY_LISTS` and :meth:`WorldCheckProvider._to_entry`; if your tenant
returns different keys, those are the three places to change and nothing else
in the package needs to know. The client sends a screening request and reads
results; that much is stable.

Credentials are never held in configuration — see :func:`aml.config.resolve_secret`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from collections.abc import Mapping, Sequence
from decimal import Decimal
from email.utils import formatdate
from typing import Any

from aml.config import ScreeningConfig, resolve_secret
from aml.http import HttpClient, HttpError
from aml.model.enums import ListType
from aml.screening.base import ListEntry, Subject

__all__ = ["WorldCheckProvider", "STRENGTH_SCORES", "CATEGORY_LISTS"]

log = logging.getLogger("aml.screening.worldcheck")

#: The vendor's qualitative match strength, mapped onto this package's scale.
#: Used as a floor by the local scorer, never as the decision — see
#: ``aml.screening.matcher``.
STRENGTH_SCORES: Mapping[str, Decimal] = {
    "EXACT": Decimal("0.99"),
    "STRONG": Decimal("0.93"),
    "MEDIUM": Decimal("0.85"),
    "WEAK": Decimal("0.70"),
    "UNKNOWN": Decimal("0.60"),
}

#: World-Check categories mapped to what the obligation is. The distinction
#: that matters: a sanctions category means freeze and report, everything else
#: means investigate.
CATEGORY_LISTS: Mapping[str, ListType] = {
    "SANCTIONS": ListType.SANCTIONS,
    "SANCTION": ListType.SANCTIONS,
    "PEP": ListType.PEP,
    "POLITICALLY EXPOSED PERSON": ListType.PEP,
    "LAW ENFORCEMENT": ListType.LAW_ENFORCEMENT,
    "REGULATORY ENFORCEMENT": ListType.REGULATORY_ENFORCEMENT,
    "ADVERSE MEDIA": ListType.ADVERSE_MEDIA,
    "OTHER BODIES": ListType.OTHER,
}


class WorldCheckProvider:
    """Screening candidates from World-Check One."""

    name = "worldcheck"

    def __init__(self, config: ScreeningConfig, client: HttpClient | None = None) -> None:
        self.config = config
        self.base_url = config.worldcheck_base_url.rstrip("/")
        self.client = client or HttpClient(
            timeout=config.request_timeout_seconds, max_retries=config.max_retries
        )
        self._lists: set[str] = set()

    # -- authentication --------------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        key = resolve_secret(self.config.worldcheck_api_key, label="World-Check API key")
        secret = resolve_secret(self.config.worldcheck_api_secret, label="World-Check API secret")
        group = resolve_secret(self.config.worldcheck_group_id, label="World-Check group id")
        if not key or not secret or not group:
            raise HttpError("World-Check credentials are not configured")
        return key, secret, group

    def _headers(self, method: str, url: str, body: bytes | None) -> dict[str, str]:
        key, secret, _ = self._credentials()
        parsed = urllib.parse.urlparse(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        date = formatdate(usegmt=True)
        lines = [
            f"(request-target): {method.lower()} {target}",
            f"host: {parsed.hostname}",
            f"date: {date}",
        ]
        signed = "(request-target) host date"
        if body is not None:
            lines += ["content-type: application/json", f"content-length: {len(body)}"]
            signed += " content-type content-length"
        digest = hmac.new(
            secret.encode("utf-8"), "\n".join(lines).encode("utf-8"), hashlib.sha256
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        headers = {
            "Date": date,
            "Authorization": (
                f'Signature keyId="{key}",algorithm="hmac-sha256",'
                f'headers="{signed}",signature="{signature}"'
            ),
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        return headers

    # -- screening -------------------------------------------------------

    def candidates(self, subject: Subject, limit: int = 25) -> Sequence[ListEntry]:
        _, _, group_id = self._credentials()
        url = f"{self.base_url}/cases/screeningRequest"
        payload: dict[str, Any] = {
            "groupId": group_id,
            "entityType": "ORGANISATION"
            if str(subject.party_type) == "ORGANIZATION"
            else "INDIVIDUAL",
            "caseId": f"aml-{subject.subject_id}",
            "providerTypes": ["WATCHLIST"],
            "name": subject.name,
        }
        secondary = self._secondary_fields(subject)
        if secondary:
            payload["secondaryFields"] = secondary

        body = json.dumps(payload).encode("utf-8")
        response = self.client.request(
            "POST", url, headers=self._headers("POST", url, body), body=body
        )
        results = self._results_from(response.json())
        entries = [self._to_entry(result, subject) for result in results[:limit]]
        self._lists.update(e.list_name for e in entries if e.list_name)
        return entries

    def _secondary_fields(self, subject: Subject) -> list[dict[str, Any]]:
        """Corroborating attributes sent with the query.

        Sending the date of birth narrows the vendor's own matching. It is sent
        as a field the vendor may use, never as a filter this package relies on
        — see the matcher on why a date of birth must not be able to exclude a
        sanctions hit by itself.
        """
        fields: list[dict[str, Any]] = []
        if subject.birth_date:
            fields.append(
                {
                    "typeId": "SFCT_1",
                    "dateTimeValue": f"{subject.birth_date.isoformat()}T00:00:00Z",
                }
            )
        if subject.nationality:
            fields.append({"typeId": "SFCT_2", "value": subject.nationality})
        return fields

    @staticmethod
    def _results_from(payload: Any) -> list[Mapping[str, Any]]:
        """Pull the result list out of whichever envelope the tenant returns."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("results", "resultsOfMatches", "matches", "content", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, Mapping)]
        log.warning("unrecognised World-Check payload shape: %s", type(payload).__name__)
        return []

    def _to_entry(self, result: Mapping[str, Any], subject: Subject) -> ListEntry:
        strength = str(
            result.get("matchStrength") or result.get("match_strength") or "UNKNOWN"
        ).upper()
        categories = result.get("categories") or result.get("category") or []
        if isinstance(categories, str):
            categories = [categories]
        list_type = ListType.OTHER
        for category in categories:
            mapped = CATEGORY_LISTS.get(str(category).upper())
            if mapped is not None:
                list_type = mapped
                break
        matched = result.get("matchedTerm") or result.get("name") or subject.name
        dates = result.get("secondaryFieldResults") or []
        birth_dates = tuple(
            str(field.get("dateTimeValue") or field.get("value"))
            for field in dates
            if isinstance(field, Mapping)
            and str(field.get("typeId", "")).upper() in {"SFCT_1", "DATE_OF_BIRTH"}
            and (field.get("dateTimeValue") or field.get("value"))
        )
        return ListEntry(
            entry_id=str(
                result.get("referenceId") or result.get("resultId") or result.get("id") or matched
            ),
            name=str(matched),
            list_type=list_type,
            list_name=str(result.get("sourceName") or "World-Check"),
            provider=self.name,
            aliases=tuple(
                str(a) for a in (result.get("aliases") or result.get("alternativeNames") or [])
            ),
            entity_type=str(result.get("entityType") or "INDIVIDUAL"),
            birth_dates=birth_dates,
            countries=tuple(str(c) for c in (result.get("countries") or [])),
            designations=tuple(str(c) for c in categories),
            remarks=str(result.get("remarks") or ""),
            provider_score=STRENGTH_SCORES.get(strength),
            source_reference=str(result.get("resultId") or ""),
            raw=dict(result),
        )

    def lists(self) -> Sequence[str]:
        return tuple(sorted(self._lists)) or ("World-Check",)
