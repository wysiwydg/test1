"""Screening against lists the institution holds itself.

This provider is not a fallback for when the vendor feed is unavailable. It is
the one that carries the obligations a vendor cannot discharge:

*   **Designations that must be acted on immediately.** UN Security Council
    consolidated listings and Philippine domestic designations require freezing
    without delay. An institution has to be able to screen against the list it
    was served, on the day it was served, without waiting for a vendor to
    ingest it.
*   **Internal watchlists.** Declined applicants, previously reported clients,
    known syndicate names from the institution's own investigations. No vendor
    has these.

Matching a whole book against a whole list is a quadratic problem, so entries
are indexed by name token and by phonetic token and only entries sharing one
are ever scored — the same blocking idea the MDM uses, for the same reason.
Recall is protected by indexing both the literal and the sound-alike form.
"""

from __future__ import annotations

import csv
import pathlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence

from aml.model.enums import ListType
from aml.screening.base import ListEntry, Subject
from aml.screening.names import name_tokens, phonetic_key

__all__ = ["LocalListProvider", "load_csv_list", "load_un_consolidated_xml"]

_MULTI = ";"


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = [p.strip() for chunk in str(value).split(_MULTI) for p in chunk.split("|")]
    return tuple(p for p in parts if p)


def load_csv_list(
    path: str | pathlib.Path,
    list_type: ListType = ListType.INTERNAL_WATCHLIST,
    list_name: str = "",
) -> list[ListEntry]:
    """Load a watch list from CSV.

    Only ``name`` is required. Everything else improves precision, and the
    columns are named rather than positional so an institution can export what
    it has without reshaping it first.
    """
    entries: list[ListEntry] = []
    file = pathlib.Path(path)
    with open(file, newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            clean = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            name = clean.get("name") or clean.get("full_name") or ""
            if not name:
                continue
            entries.append(
                ListEntry(
                    entry_id=clean.get("entry_id") or f"{file.stem}-{index}",
                    name=name,
                    list_type=ListType(clean["list_type"])
                    if clean.get("list_type") in set(ListType)
                    else list_type,
                    list_name=clean.get("list_name") or list_name or file.stem,
                    provider="local",
                    aliases=_split(clean.get("aliases")),
                    entity_type=clean.get("entity_type") or "INDIVIDUAL",
                    birth_dates=_split(clean.get("birth_dates") or clean.get("birth_date")),
                    nationalities=_split(clean.get("nationalities") or clean.get("nationality")),
                    countries=_split(clean.get("countries") or clean.get("country")),
                    id_numbers=_split(clean.get("id_numbers") or clean.get("id_number")),
                    designations=_split(clean.get("designations") or clean.get("programme")),
                    positions=_split(clean.get("positions") or clean.get("position")),
                    remarks=clean.get("remarks", ""),
                    listed_on=clean.get("listed_on", ""),
                    source_reference=str(file),
                )
            )
    return entries


def _text(node: ET.Element, *names: str) -> str:
    for name in names:
        found = node.findtext(name)
        if found and found.strip():
            return found.strip()
    return ""


def load_un_consolidated_xml(path: str | pathlib.Path) -> list[ListEntry]:
    """Load the UN Security Council consolidated list.

    Parsed defensively. The schema has changed before and will again, and a
    sanctions list that fails to load because one optional element moved is
    worse than one that loads with a field missing — so unknown shapes are
    skipped per record rather than raising for the file.
    """
    file = pathlib.Path(path)
    root = ET.parse(file).getroot()
    entries: list[ListEntry] = []

    def name_of(node: ET.Element) -> str:
        parts = [
            _text(node, "FIRST_NAME"),
            _text(node, "SECOND_NAME"),
            _text(node, "THIRD_NAME"),
            _text(node, "FOURTH_NAME"),
        ]
        return " ".join(p for p in parts if p).strip()

    for individual in root.iter("INDIVIDUAL"):
        name = name_of(individual)
        if not name:
            continue
        aliases = tuple(
            alias.strip()
            for alias in (
                node.findtext("ALIAS_NAME") or "" for node in individual.iter("INDIVIDUAL_ALIAS")
            )
            if alias and alias.strip()
        )
        births = tuple(
            born
            for born in (
                _text(node, "DATE", "YEAR", "FROM_YEAR")
                for node in individual.iter("INDIVIDUAL_DATE_OF_BIRTH")
            )
            if born
        )
        nationalities = tuple(
            value.strip()
            for node in individual.iter("NATIONALITY")
            for value in (node.itertext())
            if value.strip()
        )
        documents = tuple(
            _text(node, "NUMBER")
            for node in individual.iter("INDIVIDUAL_DOCUMENT")
            if _text(node, "NUMBER")
        )
        entries.append(
            ListEntry(
                entry_id=_text(individual, "DATAID", "REFERENCE_NUMBER") or name,
                name=name,
                list_type=ListType.UN_DESIGNATED,
                list_name="UN Security Council Consolidated List",
                provider="local",
                aliases=aliases,
                entity_type="INDIVIDUAL",
                birth_dates=births,
                nationalities=nationalities,
                id_numbers=documents,
                designations=(_text(individual, "UN_LIST_TYPE"),)
                if _text(individual, "UN_LIST_TYPE")
                else (),
                remarks=_text(individual, "COMMENTS1"),
                listed_on=_text(individual, "LISTED_ON"),
                source_reference=str(file),
            )
        )

    for entity in root.iter("ENTITY"):
        name = _text(entity, "FIRST_NAME") or name_of(entity)
        if not name:
            continue
        aliases = tuple(
            alias.strip()
            for alias in (
                node.findtext("ALIAS_NAME") or "" for node in entity.iter("ENTITY_ALIAS")
            )
            if alias and alias.strip()
        )
        entries.append(
            ListEntry(
                entry_id=_text(entity, "DATAID", "REFERENCE_NUMBER") or name,
                name=name,
                list_type=ListType.UN_DESIGNATED,
                list_name="UN Security Council Consolidated List",
                provider="local",
                aliases=aliases,
                entity_type="ENTITY",
                remarks=_text(entity, "COMMENTS1"),
                listed_on=_text(entity, "LISTED_ON"),
                source_reference=str(file),
            )
        )
    return entries


class LocalListProvider:
    """Blocking index over locally held list entries."""

    name = "local"

    def __init__(self, entries: Iterable[ListEntry] = ()) -> None:
        self.entries: tuple[ListEntry, ...] = tuple(entries)
        self._by_token: dict[str, set[int]] = {}
        self._by_sound: dict[str, set[int]] = {}
        for position, entry in enumerate(self.entries):
            for value in entry.all_names:
                for token in name_tokens(value):
                    self._by_token.setdefault(token, set()).add(position)
                for sound in phonetic_key(value).split():
                    self._by_sound.setdefault(sound, set()).add(position)

    @classmethod
    def from_files(cls, files: Mapping[str, str]) -> LocalListProvider:
        """Load every configured list. Keys are :class:`ListType` names."""
        entries: list[ListEntry] = []
        for list_type_name, path in files.items():
            file = pathlib.Path(path).expanduser()
            if not file.exists():
                raise FileNotFoundError(f"screening list not found: {file}")
            try:
                list_type = ListType(list_type_name.upper())
            except ValueError:
                list_type = ListType.OTHER
            if file.suffix.lower() == ".xml":
                entries.extend(load_un_consolidated_xml(file))
            else:
                entries.extend(load_csv_list(file, list_type=list_type))
        return cls(entries)

    def lists(self) -> Sequence[str]:
        return tuple(sorted({e.list_name for e in self.entries if e.list_name}))

    def candidates(self, subject: Subject, limit: int = 25) -> Sequence[ListEntry]:
        """Entries sharing a name token or a sound-alike token with the subject."""
        counts: dict[int, int] = {}
        for value in subject.all_names:
            for token in name_tokens(value):
                for position in self._by_token.get(token, ()):
                    counts[position] = counts.get(position, 0) + 2
            for sound in phonetic_key(value).split():
                for position in self._by_sound.get(sound, ()):
                    counts[position] = counts.get(position, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return tuple(self.entries[position] for position, _ in ranked[:limit])

    def __len__(self) -> int:
        return len(self.entries)
