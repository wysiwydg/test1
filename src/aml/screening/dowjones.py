"""Dow Jones Risk & Compliance as a candidate source.

Bearer-token rather than request signing: a token is obtained from the Dow
Jones OAuth endpoint and reused until it expires. Two grant types are supported
because tenants are provisioned differently — ``client_credentials`` where the
institution has a service account, and ``password`` where it has named user
credentials. Which one applies is a property of the contract, not of this code,
so it is configuration.

**On field mapping**, the same caution as the World-Check module: the search
path and the response keys are tenant- and version-specific. Everything that
depends on them is in :data:`CONTENT_SET_LISTS` and :meth:`_to_entry`.

The token is held in memory only, refreshed a minute before expiry, and never
written to the store or the logs.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from aml.config import ScreeningConfig, resolve_secret
from aml.http import HttpClient, HttpError
from aml.model.enums import ListType
from aml.screening.base import ListEntry, Subject

__all__ = ["DowJonesProvider", "CONTENT_SET_LISTS"]

log = logging.getLogger("aml.screening.dowjones")

#: Dow Jones content sets mapped to what each one obliges the institution to do.
CONTENT_SET_LISTS: Mapping[str, ListType] = {
    "SANCTIONS_LIST": ListType.SANCTIONS,
    "SANCTIONS": ListType.SANCTIONS,
    "SANCTIONS_CONTROL_LIST": ListType.SANCTIONS,
    "PEP": ListType.PEP,
    "PERSON_OF_INTEREST": ListType.LAW_ENFORCEMENT,
    "ADVERSE_MEDIA": ListType.ADVERSE_MEDIA,
    "AME": ListType.ADVERSE_MEDIA,
    "OTHER_EXCLUSION_LIST": ListType.REGULATORY_ENFORCEMENT,
    "RELATIONSHIP": ListType.OTHER,
}


class DowJonesProvider:
    """Screening candidates from the Dow Jones Risk & Compliance API."""

    name = "dowjones"

    def __init__(
        self,
        config: ScreeningConfig,
        client: HttpClient | None = None,
        grant_type: str = "client_credentials",
    ) -> None:
        self.config = config
        self.base_url = config.dowjones_base_url.rstrip("/")
        self.grant_type = grant_type
        self.client = client or HttpClient(
            timeout=config.request_timeout_seconds, max_retries=config.max_retries
        )
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lists: set[str] = set()

    # -- authentication --------------------------------------------------

    def _token_header(self) -> dict[str, str]:
        if self._token and time.time() < self._expires_at:
            return {"Authorization": f"Bearer {self._token}"}

        client_id = resolve_secret(self.config.dowjones_client_id, label="Dow Jones client id")
        client_secret = resolve_secret(
            self.config.dowjones_client_secret, label="Dow Jones client secret"
        )
        if not client_id or not client_secret:
            raise HttpError("Dow Jones credentials are not configured")

        fields: dict[str, str] = {
            "grant_type": self.grant_type,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid service_account_id",
        }
        if self.grant_type == "password":
            username = resolve_secret(self.config.dowjones_username, label="Dow Jones username")
            password = resolve_secret(self.config.dowjones_password, label="Dow Jones password")
            fields.update({"username": username or "", "password": password or "",
                           "connection": "service-account"})

        response = self.client.post_form(self.config.dowjones_token_url, fields)
        payload = response.json() or {}
        token = (
            payload.get("access_token")
            or payload.get("id_token")
            or payload.get("token")
        )
        if not token:
            raise HttpError("Dow Jones token endpoint returned no token", status=response.status)
        # A minute of headroom: a token that expires mid-batch fails the
        # request that happens to be in flight, which is the one that then
        # looks like a screening gap.
        self._token = str(token)
        self._expires_at = time.time() + float(payload.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    # -- screening -------------------------------------------------------

    def candidates(self, subject: Subject, limit: int = 25) -> Sequence[ListEntry]:
        query: dict[str, str] = {
            "filter[name]": subject.name,
            "filter[record-type]": "entity"
            if str(subject.party_type) == "ORGANIZATION"
            else "person",
            "page[limit]": str(limit),
        }
        if subject.birth_date:
            query["filter[date-of-birth]"] = subject.birth_date.isoformat()
        if subject.country:
            query["filter[country]"] = subject.country

        url = f"{self.base_url}/search?{urllib.parse.urlencode(query)}"
        headers = {"Accept": "application/json", **self._token_header()}
        response = self.client.get(url, headers=headers)
        payload = response.json() or {}
        records = payload.get("data") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            log.warning("unrecognised Dow Jones payload shape: %s", type(payload).__name__)
            return ()
        entries = [
            self._to_entry(record) for record in records[:limit] if isinstance(record, Mapping)
        ]
        self._lists.update(e.list_name for e in entries if e.list_name)
        return entries

    def _to_entry(self, record: Mapping[str, Any]) -> ListEntry:
        attributes = record.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            attributes = {}
        content_sets = attributes.get("content_set") or attributes.get("content_sets") or []
        if isinstance(content_sets, str):
            content_sets = [content_sets]
        list_type = ListType.OTHER
        for content_set in content_sets:
            mapped = CONTENT_SET_LISTS.get(str(content_set).upper())
            if mapped is not None:
                list_type = mapped
                break
        score = attributes.get("score") or attributes.get("match_score")
        return ListEntry(
            entry_id=str(record.get("id") or attributes.get("profile_id") or ""),
            name=str(
                attributes.get("primary_name")
                or attributes.get("name")
                or attributes.get("title")
                or ""
            ),
            list_type=list_type,
            list_name="Dow Jones Risk & Compliance",
            provider=self.name,
            aliases=tuple(str(a) for a in (attributes.get("also_known_as") or [])),
            entity_type="ENTITY"
            if str(attributes.get("record_type", "")).lower() == "entity"
            else "INDIVIDUAL",
            birth_dates=tuple(
                str(d) for d in (attributes.get("date_of_birth") or attributes.get("dates") or [])
            )
            if isinstance(attributes.get("date_of_birth") or attributes.get("dates"), list)
            else tuple(
                str(d) for d in [attributes.get("date_of_birth")] if d
            ),
            countries=tuple(str(c) for c in (attributes.get("countries") or [])),
            designations=tuple(str(c) for c in content_sets),
            positions=tuple(str(p) for p in (attributes.get("titles") or [])),
            remarks=str(attributes.get("summary") or ""),
            provider_score=None
            if score in (None, "")
            else (Decimal(str(score)) / 100 if Decimal(str(score)) > 1 else Decimal(str(score))),
            source_reference=str(record.get("id") or ""),
            raw=dict(record),
        )

    def lists(self) -> Sequence[str]:
        return tuple(sorted(self._lists)) or ("Dow Jones Risk & Compliance",)
