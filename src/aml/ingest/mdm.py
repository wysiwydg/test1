"""Reading customers from the Customer MDM golden store.

Optional, and worth explaining. This package runs perfectly well against a flat
extract, and most institutions will start there. But an insurer's clients
routinely appear several times across policy administration, claims and agency
systems, and monitoring a client's activity *per source system* is monitoring
the wrong subject: three policies under three source identities never aggregate
to a covered transaction, and a sanctions hit on one of them leaves the other
two unscreened.

When the MDM in this repository is deployed, the golden person is the right
subject. This adapter reads it, and the ``mdm_person_id`` carried on
:class:`~aml.model.entities.Party` is what ties an alert back to the record the
stewards curate.

``psycopg`` is imported inside the function so the whole AML package stays
importable — and testable — with no database driver installed.
"""

from __future__ import annotations

from aml.model.entities import IdDocument, Party
from aml.model.enums import IdDocumentType, PartyType, RiskRating

__all__ = ["load_parties_from_mdm"]

_QUERY = """
SELECT person_id,
       party_type,
       full_name,
       first_name,
       middle_name,
       last_name,
       name_suffix,
       date_of_birth,
       nationality,
       address_line1,
       city,
       state_province,
       postal_code,
       country,
       phone_e164,
       email_normalized,
       national_id_type,
       national_id_number,
       occupation
  FROM mdm.person
 WHERE is_current
   AND COALESCE(is_active, TRUE)
 ORDER BY person_id
 LIMIT %(limit)s
"""


def load_parties_from_mdm(dsn: str, *, limit: int = 100_000) -> list[Party]:
    """Load golden persons as AML parties.

    Column names follow the MDM's canonical registry. Where the golden record
    does not carry an attribute this package wants — declared income, PEP
    status, source of funds — the field is left unset rather than invented; it
    comes from the KYC file, and the loader for that is
    :func:`aml.ingest.loader.load_parties`.
    """
    import psycopg
    from psycopg.rows import dict_row

    parties: list[Party] = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for row in conn.execute(_QUERY, {"limit": limit}):
            documents = []
            if row.get("national_id_number"):
                try:
                    doc_type = IdDocumentType(str(row.get("national_id_type") or "OTHER").upper())
                except ValueError:
                    doc_type = IdDocumentType.OTHER
                documents.append(
                    IdDocument(doc_type=doc_type, number=str(row["national_id_number"]))
                )
            parties.append(
                Party(
                    party_id=str(row["person_id"]),
                    party_type=PartyType.ORGANIZATION
                    if str(row.get("party_type", "")).upper().startswith("ORG")
                    else PartyType.INDIVIDUAL,
                    full_name=str(row.get("full_name") or ""),
                    first_name=str(row.get("first_name") or ""),
                    middle_name=str(row.get("middle_name") or ""),
                    last_name=str(row.get("last_name") or ""),
                    suffix=str(row.get("name_suffix") or ""),
                    birth_date=row.get("date_of_birth"),
                    nationality=str(row.get("nationality") or "PH"),
                    address_line=str(row.get("address_line1") or ""),
                    city=str(row.get("city") or ""),
                    province=str(row.get("state_province") or ""),
                    postal_code=str(row.get("postal_code") or ""),
                    country=str(row.get("country") or "PH"),
                    phone=str(row.get("phone_e164") or ""),
                    email=str(row.get("email_normalized") or ""),
                    identifications=tuple(documents),
                    occupation=str(row.get("occupation") or ""),
                    risk_rating=RiskRating.UNRATED,
                    mdm_person_id=str(row["person_id"]),
                    source_system="cmdm",
                )
            )
    return parties
