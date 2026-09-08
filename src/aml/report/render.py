"""Writing the file, and knowing exactly what was written.

Rendering is deterministic: the same records through the same spec produce
byte-identical output. That is not tidiness. The digest of the rendered file is
what the submission ledger records, what the package manifest carries, and what
proves years later that the file the AMLC holds is the file this system
produced. A timestamp inside the payload would break that, so there isn't one —
the filing dates are fields, set by the builder, not by the writer.
"""

from __future__ import annotations

import csv
import hashlib
import io
import pathlib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aml.report.spec import ReportSpec

__all__ = ["RenderedFile", "render", "render_csv", "render_xml", "file_digest"]


@dataclass(frozen=True, slots=True)
class RenderedFile:
    """A written report file and its identity."""

    path: pathlib.Path
    sha256: str
    rows: int
    size_bytes: int
    kind: str
    spec_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "sha256": self.sha256,
            "rows": self.rows,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "spec_version": self.spec_version,
        }


def file_digest(path: str | pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_csv(records: Sequence[Mapping[str, Any]], spec: ReportSpec) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=spec.delimiter,
        lineterminator=spec.line_terminator,
        quoting=csv.QUOTE_MINIMAL,
    )
    if spec.include_header:
        writer.writerow(spec.columns)
    for record in records:
        writer.writerow(spec.row(record))
    return buffer.getvalue().encode(spec.encoding)


def render_xml(records: Sequence[Mapping[str, Any]], spec: ReportSpec) -> bytes:
    root = ET.Element(spec.xml_root)
    for record in records:
        node = ET.SubElement(root, spec.xml_record)
        for field, value in zip(spec.fields, spec.row(record), strict=True):
            child = ET.SubElement(node, field.column)
            child.text = value
    ET.indent(root, space="  ")
    payload: bytes = ET.tostring(root, encoding=spec.encoding, xml_declaration=True)
    return payload


def render(
    records: Sequence[Mapping[str, Any]],
    spec: ReportSpec,
    path: str | pathlib.Path,
) -> RenderedFile:
    """Write the records to ``path`` in the spec's format."""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        render_xml(records, spec)
        if spec.file_extension.lower() == "xml"
        else render_csv(records, spec)
    )
    target.write_bytes(payload)
    return RenderedFile(
        path=target,
        sha256=hashlib.sha256(payload).hexdigest(),
        rows=len(records),
        size_bytes=len(payload),
        kind=str(spec.kind),
        spec_version=spec.version,
    )
