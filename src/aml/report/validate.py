"""Validating a report before it is submitted, not after it is rejected.

The AMLC portal rejects a malformed file, and a rejection two days before a
deadline costs a working day of a compliance unit's week. Every check here is
one the regulator's own intake would apply, run locally, in a form that names
the record and the column rather than returning a row number.

The severities matter. A missing mandatory field is an ``error`` and blocks
submission. A truncation risk or an unmapped code is a ``warning``: it may be
perfectly acceptable, but somebody should look before it goes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aml.report.spec import ReportSpec

__all__ = ["Issue", "validate_records", "blocking"]


@dataclass(frozen=True, slots=True)
class Issue:
    row: int
    column: str
    code: str
    message: str
    severity: str = "error"
    reference: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "reference": self.reference,
        }

    def __str__(self) -> str:
        where = f"row {self.row}" + (f" ({self.reference})" if self.reference else "")
        return f"[{self.severity}] {where} {self.column}: {self.message}"


def validate_records(
    records: Sequence[Mapping[str, Any]], spec: ReportSpec
) -> list[Issue]:
    """Check every record against the layout it will be rendered through."""
    issues: list[Issue] = []
    seen_references: dict[str, int] = {}
    # Quoting warnings are per column, not per row: an institution name with a
    # comma in it would otherwise produce one warning per record and bury the
    # findings that matter.
    quoting_warned: set[str] = set()

    for row, record in enumerate(records, start=1):
        reference = str(record.get("report_reference", ""))
        if reference:
            if reference in seen_references:
                issues.append(
                    Issue(
                        row,
                        "REPORT_REFERENCE",
                        "duplicate_reference",
                        f"reference {reference} already used by row {seen_references[reference]}",
                        reference=reference,
                    )
                )
            else:
                seen_references[reference] = row

        for field in spec.fields:
            raw = record.get(field.source)
            rendered = field.render(raw)

            if field.required and not rendered.strip():
                issues.append(
                    Issue(
                        row,
                        field.column,
                        "missing_required",
                        f"{field.column} is mandatory and is empty"
                        + (f" — {field.doc}" if field.doc else ""),
                        reference=reference,
                    )
                )
                continue

            if not rendered:
                continue

            if field.max_length and len(rendered) > field.max_length:
                issues.append(
                    Issue(
                        row,
                        field.column,
                        "too_long",
                        f"{len(rendered)} characters exceeds the {field.max_length} the "
                        "schema allows; it would be truncated or rejected",
                        severity="error" if field.required else "warning",
                        reference=reference,
                    )
                )

            if field.pattern and not re.match(field.pattern, rendered):
                issues.append(
                    Issue(
                        row,
                        field.column,
                        "pattern",
                        f"{rendered!r} does not match {field.pattern}",
                        reference=reference,
                    )
                )

            if field.dtype == "enum" and field.values and str(raw) not in field.values:
                issues.append(
                    Issue(
                        row,
                        field.column,
                        "unmapped_code",
                        f"{raw!r} has no mapping in the layout and is being filed verbatim",
                        severity="warning",
                        reference=reference,
                    )
                )

            if (
                spec.delimiter
                and spec.delimiter in rendered
                and spec.file_extension == "csv"
                and field.column not in quoting_warned
            ):
                quoting_warned.add(field.column)
                # Quoting handles this correctly, but some intake systems do not
                # honour quoting, so it is worth a look rather than a surprise.
                issues.append(
                    Issue(
                        row,
                        field.column,
                        "delimiter_in_value",
                        f"value contains the {spec.delimiter!r} delimiter and will be quoted",
                        severity="warning",
                        reference=reference,
                    )
                )

    return issues


def blocking(issues: Sequence[Issue]) -> list[Issue]:
    """The issues that must be fixed before the file may be submitted."""
    return [i for i in issues if i.severity == "error"]
