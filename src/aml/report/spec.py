"""The report layout, declared as data.

This is the most important design decision in the reporting half of the
package, so it is worth being explicit about why.

A regulator's file layout is not a stable interface. Columns are added,
renamed, reordered and re-coded, and a covered person finds out when a
submission is rejected — usually days before a deadline. A layout compiled into
Python is a layout that needs a developer, a release and a change window to
follow the regulator. A layout declared in a TOML file is one a compliance
officer changes on the afternoon the guidance is published, with the validator
telling them immediately whether the result is well formed.

So the pipeline is: detection produces *canonical* records with names this
package chooses, and a spec projects those onto whatever the AMLC's current
schema calls them, in whatever order, with whatever formatting and codes. The
canonical side never changes when the regulator's does.

**The layouts that ship here are a working default, not the authority.** They
carry the particulars the AMLA and its implementing rules require a report to
contain — the identity of the client, the transaction, the amounts, the
suspicious circumstances — under sensible column names. Before the first live
submission they must be reconciled with the schema published with the AMLC's
current registration and reporting guidelines for the institution's covered
person type; ``aml.report.validate`` will tell you what is missing, but only
the published schema can tell you what to call it.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aml.model.enums import ReportKind
from aml.money import Money

__all__ = ["FieldSpec", "ReportSpec", "SPEC_DIR", "load_spec"]

SPEC_DIR = pathlib.Path(__file__).parent / "specs"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One column of the filed report."""

    #: The regulator's column name. What appears in the file.
    column: str
    #: The canonical key this package produces. What the builders emit.
    source: str
    dtype: str = "text"
    required: bool = False
    max_length: int | None = None
    pattern: str = ""
    #: Canonical value to regulator code, for coded fields.
    values: Mapping[str, str] = field(default_factory=dict)
    #: ``strftime`` pattern for dates; number of decimal places for amounts.
    format: str = ""
    default: str = ""
    doc: str = ""

    def render(self, value: Any) -> str:
        """Format one value the way the schema wants it."""
        if value is None or value == "":
            return self.default
        if self.dtype == "date":
            if isinstance(value, (dt.date, dt.datetime)):
                return value.strftime(self.format or "%Y-%m-%d")
            return str(value)
        if self.dtype == "datetime":
            if isinstance(value, dt.datetime):
                return value.strftime(self.format or "%Y-%m-%d %H:%M:%S")
            return str(value)
        if self.dtype == "decimal":
            places = int(self.format or 2)
            amount = value.amount if isinstance(value, Money) else Decimal(str(value))
            return str(amount.quantize(Decimal(1).scaleb(-places)))
        if self.dtype == "integer":
            return str(int(value))
        if self.dtype == "list":
            if isinstance(value, (list, tuple, set)):
                return (self.format or "; ").join(str(v) for v in value)
            return str(value)
        if self.dtype == "enum":
            return self.values.get(str(value), str(value))
        text = str(value)
        return " ".join(text.split()) if "\n" in text or "\r" in text else text


@dataclass(frozen=True, slots=True)
class ReportSpec:
    """A complete file layout for one report kind."""

    kind: ReportKind
    version: str
    name: str
    description: str = ""
    authority: str = ""
    file_extension: str = "csv"
    delimiter: str = ","
    include_header: bool = True
    encoding: str = "utf-8"
    line_terminator: str = "\r\n"
    #: ``{institution}``, ``{kind}``, ``{date}``, ``{sequence}`` are substituted.
    filename_pattern: str = "{institution}_{kind}_{date}_{sequence}.{ext}"
    #: XML element names, used when ``file_extension`` is ``xml``.
    xml_root: str = "reports"
    xml_record: str = "report"
    fields: tuple[FieldSpec, ...] = ()

    def field_by_source(self, source: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.source == source), None)

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f.column for f in self.fields)

    @property
    def required_sources(self) -> tuple[str, ...]:
        return tuple(f.source for f in self.fields if f.required)

    def row(self, record: Mapping[str, Any]) -> list[str]:
        """Project one canonical record onto the schema's columns."""
        return [f.render(record.get(f.source)) for f in self.fields]

    def filename(self, institution_code: str, day: dt.date, sequence: int = 1) -> str:
        return self.filename_pattern.format(
            institution=institution_code or "INSTITUTION",
            kind=str(self.kind),
            date=day.strftime("%Y%m%d"),
            sequence=f"{sequence:03d}",
            ext=self.file_extension,
        )

    @classmethod
    def from_toml(cls, path: str | pathlib.Path) -> ReportSpec:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
        meta = raw.get("report", {})
        fields = tuple(
            FieldSpec(
                column=str(entry["column"]),
                source=str(entry.get("source", entry["column"])),
                dtype=str(entry.get("type", "text")),
                required=bool(entry.get("required", False)),
                max_length=int(entry["max_length"]) if entry.get("max_length") else None,
                pattern=str(entry.get("pattern", "")),
                values=dict(entry.get("values", {})),
                format=str(entry.get("format", "")),
                default=str(entry.get("default", "")),
                doc=str(entry.get("doc", "")),
            )
            for entry in raw.get("field", [])
        )
        if not fields:
            raise ValueError(f"report spec {path} declares no fields")
        return cls(
            kind=ReportKind(str(meta.get("kind", "CTR")).upper()),
            version=str(meta.get("version", "1")),
            name=str(meta.get("name", "")),
            description=str(meta.get("description", "")),
            authority=str(meta.get("authority", "")),
            file_extension=str(meta.get("file_extension", "csv")),
            delimiter=str(meta.get("delimiter", ",")),
            include_header=bool(meta.get("include_header", True)),
            encoding=str(meta.get("encoding", "utf-8")),
            line_terminator=str(meta.get("line_terminator", "\r\n")),
            filename_pattern=str(
                meta.get("filename_pattern", "{institution}_{kind}_{date}_{sequence}.{ext}")
            ),
            xml_root=str(meta.get("xml_root", "reports")),
            xml_record=str(meta.get("xml_record", "report")),
            fields=fields,
        )


def load_spec(
    kind: ReportKind | str, version: str = "v1", directory: str | pathlib.Path | None = None
) -> ReportSpec:
    """Load a layout by kind and version.

    An institution points ``directory`` at its own copy once the AMLC's
    published schema has been transcribed; the shipped specs stay as the
    reference to diff against.
    """
    base = pathlib.Path(directory) if directory else SPEC_DIR
    name = str(kind).lower()
    path = base / f"{name}_{version}.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"no {name} report spec at {path}; ship one or point --spec-dir at yours"
        )
    return ReportSpec.from_toml(path)
