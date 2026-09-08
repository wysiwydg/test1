"""Regulator code tables, held as data.

Two kinds of code live here, and they behave differently over time.

**Suspicious circumstances (ST1-ST7)** are statutory. They come from the
definition of a suspicious transaction in the AMLA as amended, and they change
only when Congress amends the law. Every detection rule in this package
declares which of them it evidences, because an STR that does not name a
circumstance is not a complete report.

**Unlawful activities** are the predicate offences enumerated in the AMLA. The
list grows by amendment — tax evasion was added years after the original Act —
so it is a table, not an enum, and the codes are internal identifiers.

A caution worth putting in the source rather than only in the manual: *the
numeric codes the AMLC's reporting forms use are not necessarily these.* The
authoritative code list is the one published with the AMLC's current
registration and reporting guidelines, and it is versioned by the AMLC, not by
this package. :func:`load_code_table` exists so an institution can point at the
published list and have the reports carry the regulator's own codes; what ships
here is a working default with the statutory text attached, so a compliance
officer can see what each code means without a second document open.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "CodeEntry",
    "SUSPICIOUS_CIRCUMSTANCES",
    "UNLAWFUL_ACTIVITIES",
    "COVERED_PERSON_TYPES",
    "describe",
    "load_code_table",
]


@dataclass(frozen=True, slots=True)
class CodeEntry:
    """One code, what it means, and where it comes from."""

    code: str
    label: str
    reference: str = ""
    note: str = ""


def _table(entries: tuple[CodeEntry, ...]) -> Mapping[str, CodeEntry]:
    return MappingProxyType({e.code: e for e in entries})


# ---------------------------------------------------------------------------
# Suspicious circumstances — the statutory definition of a suspicious
# transaction. A transaction is suspicious if *any* of these is present,
# regardless of amount. ST4 and ST5 are the two this system detects
# automatically with any confidence; the rest need a human, which is why every
# alert carries a narrative field rather than only a score.
# ---------------------------------------------------------------------------

SUSPICIOUS_CIRCUMSTANCES: Mapping[str, CodeEntry] = _table(
    (
        CodeEntry(
            "ST1",
            "No underlying legal or trade obligation, purpose or economic justification",
            "AMLA, definition of suspicious transaction",
        ),
        CodeEntry(
            "ST2",
            "Client is not properly identified",
            "AMLA, definition of suspicious transaction",
            "Incomplete or unverifiable CDD. Detectable from the customer record itself.",
        ),
        CodeEntry(
            "ST3",
            "Amount involved is not commensurate with the business or financial capacity "
            "of the client",
            "AMLA, definition of suspicious transaction",
        ),
        CodeEntry(
            "ST4",
            "Transaction is structured to avoid being the subject of reporting requirements",
            "AMLA, definition of suspicious transaction",
            "The circumstance the structuring rules evidence.",
        ),
        CodeEntry(
            "ST5",
            "Circumstance observed to deviate from the client's profile or past transactions",
            "AMLA, definition of suspicious transaction",
            "The circumstance the behavioural baseline rules evidence.",
        ),
        CodeEntry(
            "ST6",
            "Transaction is related to an unlawful activity or a money laundering or "
            "terrorist financing offence that is about to be, is being, or has been committed",
            "AMLA, definition of suspicious transaction",
            "Sanctions and adverse-media screening hits land here.",
        ),
        CodeEntry(
            "ST7",
            "Any transaction similar, analogous or identical to any of the foregoing",
            "AMLA, definition of suspicious transaction",
        ),
    )
)


# ---------------------------------------------------------------------------
# Predicate unlawful activities. Codes are internal; map them to the AMLC's
# published list at configuration time.
# ---------------------------------------------------------------------------

UNLAWFUL_ACTIVITIES: Mapping[str, CodeEntry] = _table(
    (
        CodeEntry("UA01", "Kidnapping for ransom", "Revised Penal Code, Art. 267"),
        CodeEntry("UA02", "Dangerous drugs offences", "RA 9165"),
        CodeEntry("UA03", "Graft and corrupt practices", "RA 3019"),
        CodeEntry("UA04", "Plunder", "RA 7080"),
        CodeEntry("UA05", "Robbery and extortion", "Revised Penal Code, Arts. 294-296, 299-302"),
        CodeEntry("UA06", "Jueteng and masiao", "PD 1602"),
        CodeEntry("UA07", "Piracy on the high seas", "Revised Penal Code; PD 532"),
        CodeEntry("UA08", "Qualified theft", "Revised Penal Code, Art. 310"),
        CodeEntry("UA09", "Swindling (estafa)", "Revised Penal Code, Arts. 315-316"),
        CodeEntry("UA10", "Smuggling", "RA 10863 (Customs Modernization and Tariff Act)"),
        CodeEntry("UA11", "Violations of the Electronic Commerce Act", "RA 8792"),
        CodeEntry("UA12", "Hijacking, destructive arson and murder", "RA 6235; Revised Penal Code"),
        CodeEntry("UA13", "Terrorism and conspiracy to commit terrorism", "RA 11479"),
        CodeEntry("UA14", "Financing of terrorism", "RA 10168"),
        CodeEntry("UA15", "Bribery and corruption of public officers",
                  "Revised Penal Code, Arts. 210-212"),
        CodeEntry("UA16", "Frauds and illegal exactions and transactions",
                  "Revised Penal Code, Arts. 213-216"),
        CodeEntry("UA17", "Malversation of public funds and property",
                  "Revised Penal Code, Arts. 217-218"),
        CodeEntry("UA18", "Forgeries and counterfeiting",
                  "Revised Penal Code, Arts. 163, 166-169, 176"),
        CodeEntry("UA19", "Violations of the Securities Regulation Code", "RA 8799"),
        CodeEntry("UA20", "Trafficking in persons", "RA 9208 as amended by RA 10364"),
        CodeEntry("UA21", "Child pornography", "RA 9775"),
        CodeEntry("UA22", "Violations of the Intellectual Property Code", "RA 8293"),
        CodeEntry("UA23", "Violations of the Migrant Workers and Overseas Filipinos Act",
                  "RA 8042 as amended"),
        CodeEntry("UA24", "Violations of the Revised Forestry Code", "PD 705"),
        CodeEntry("UA25", "Violations of the Philippine Fisheries Code", "RA 8550"),
        CodeEntry("UA26", "Violations of the Philippine Mining Act", "RA 7942"),
        CodeEntry("UA27", "Violations of the Wildlife Resources Conservation and Protection Act",
                  "RA 9147"),
        CodeEntry("UA28", "Violations of the National Caves and Cave Resources Act", "RA 9072"),
        CodeEntry("UA29", "Carnapping", "RA 6539"),
        CodeEntry("UA30", "Violations of the laws on firearms and explosives", "RA 10591"),
        CodeEntry("UA31", "Fencing", "PD 1612"),
        CodeEntry("UA32", "Tax evasion", "NIRC Secs. 254 and 255, as amended by RA 10963"),
        CodeEntry("UA99", "Other unlawful activity", "", "Describe in the narrative."),
    )
)


# ---------------------------------------------------------------------------
# Covered person types under the AMLA that are supervised by the Insurance
# Commission. Which one an institution is decides how it registers with the
# AMLC and what its reports declare.
# ---------------------------------------------------------------------------

COVERED_PERSON_TYPES: Mapping[str, CodeEntry] = _table(
    (
        CodeEntry("INS-LIFE", "Life insurance company", "Supervised by the Insurance Commission"),
        CodeEntry("INS-NONLIFE", "Non-life insurance company",
                  "Supervised by the Insurance Commission"),
        CodeEntry("INS-PRENEED", "Pre-need company", "Supervised by the Insurance Commission"),
        CodeEntry("INS-MBA", "Mutual benefit association",
                  "Supervised by the Insurance Commission"),
        CodeEntry("INS-AGENT", "Insurance agent", "Supervised by the Insurance Commission"),
        CodeEntry("INS-BROKER", "Insurance broker", "Supervised by the Insurance Commission"),
        CodeEntry("INS-REINS", "Professional reinsurer or reinsurance broker",
                  "Supervised by the Insurance Commission"),
        CodeEntry("INS-HOLDING", "Insurance holding company or holding company system",
                  "Supervised by the Insurance Commission"),
    )
)


def describe(code: str) -> CodeEntry | None:
    """Look a code up in whichever table owns it."""
    for table in (SUSPICIOUS_CIRCUMSTANCES, UNLAWFUL_ACTIVITIES, COVERED_PERSON_TYPES):
        entry = table.get(code)
        if entry is not None:
            return entry
    return None


def load_code_table(path: str | Path) -> Mapping[str, CodeEntry]:
    """Read a regulator-published code list from TOML.

    Expected shape, one table per code::

        [UA33]
        label = "Some newly added predicate offence"
        reference = "RA 12345"

    Used to carry the AMLC's own numbering into reports without editing code.
    """
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    return _table(
        tuple(
            CodeEntry(
                code=code,
                label=str(body.get("label", "")),
                reference=str(body.get("reference", "")),
                note=str(body.get("note", "")),
            )
            for code, body in raw.items()
            if isinstance(body, dict)
        )
    )
