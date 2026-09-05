"""Convert zipped SAS7BDAT extracts into CSV that Azure Postgres can COPY.

    python -m scripts.sas2csv EXTRACT.zip --out C:\\staging

Written for a Windows machine with Python 3.13, no internet, and extracts of the
shape this project receives them in: half a million rows, two hundred-odd
columns, delivered as a zip. Four decisions define it.

**Nothing is extracted.** The sas7bdat is read as a stream straight out of the
zip. A 500k x 200 extract is 800 MB of doubles uncompressed and compresses about
five-fold, so extracting first asks for several GB of scratch space per file and
leaves a copy of the source data on disk to be cleaned up afterwards. The format
allows the stream because the reader only ever moves forward: one seek to byte
zero, then page-sized reads to the end. That was measured, not assumed -- see
``tests/test_sas2csv.py::test_streaming_and_extracting_agree``.

**Types are inferred from the data, not from SAS.** SAS has one numeric type: an
8-byte float. Projecting that onto ``double precision`` is faithful to the
storage and useless in a database, because every account number and policy count
arrives as a float and every key built on one is a float key. So the run
accumulates min, max, integrality and decimal scale per column as it streams, and
the DDL it writes afterwards names the narrowest Postgres type that holds what
was actually there: ``smallint``/``integer``/``bigint`` for whole numbers,
``numeric(p,s)`` where a stable scale fits -- money, almost always -- and
``double precision`` only where the values genuinely need a binary float.

**Dates are converted, and the ambiguous formats are not.** A SAS date is days
since 1960-01-01 and a SAS datetime is seconds since it, tagged by a display
format. ``DATE9.``, ``MMDDYY10.`` and ``DATETIME20.`` are unambiguous and become
``date`` and ``timestamp``. ``YEAR4.``, ``MONTH.`` and ``DAY.`` are not: the
stored value is a date, but those formats are also what somebody reaches for when
displaying a plain integer year, and reading 2024 as a date silently yields
1965-07-17. Those stay numeric unless ``--date-formats broad`` or
``--treat-as-date`` says otherwise, and the run reports every column it left
behind on that basis rather than deciding quietly.

**The conversion is one pass, and it says what it is doing.** A 500k-row file is
minutes of work; a converter that prints nothing for minutes is indistinguishable
from one that has hung.

The output is a CSV per dataset, a ``CREATE TABLE`` beside it, and a ``load.sql``
that runs the pair through ``psql``. The CSV is UTF-8 with no BOM, ``\\n``
endings, a header row, and empty for null -- which is what ``COPY ... WITH
(FORMAT csv, HEADER true, NULL '')`` reads, and what the generated load script
asks for.

Needs pandas and numpy. pyreadstat is optional and only for ``--reader fast``,
which trades the no-extraction property for roughly five times the throughput by
spilling the member to a temporary file first.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import csv
import dataclasses
import io
import pathlib
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "Column",
    "Dataset",
    "Options",
    "convert",
    "normalise_format",
    "postgres_type",
    "sql_identifier",
]

# --------------------------------------------------------------------------- #
# SAS display formats
#
# The value under a date format is a date whatever the format displays, so
# reading MONYY7. as a date loses nothing. The exception is the formats that
# render one *component* -- YEAR, MONTH, DAY, QTR -- because those are equally
# what gets attached to a plain integer, and there is no way to tell the two
# apart from the file. They are listed separately and only honoured under
# --date-formats broad.
# --------------------------------------------------------------------------- #

DATE_FORMATS = frozenset({
    "DATE", "DDMMYY", "DDMMYYB", "DDMMYYC", "DDMMYYD", "DDMMYYN", "DDMMYYP",
    "DDMMYYS", "MMDDYY", "MMDDYYB", "MMDDYYC", "MMDDYYD", "MMDDYYN", "MMDDYYP",
    "MMDDYYS", "YYMMDD", "YYMMDDB", "YYMMDDC", "YYMMDDD", "YYMMDDN", "YYMMDDP",
    "YYMMDDS", "MMYY", "MMYYC", "MMYYD", "MMYYN", "MMYYP", "MMYYS", "YYMM",
    "YYMMC", "YYMMD", "YYMMN", "YYMMP", "YYMMS", "MONYY", "YYMON", "YYQ",
    "YYQC", "YYQD", "YYQN", "YYQP", "YYQS", "YYQR", "YYQRC", "YYQRD", "YYQRN",
    "YYQRP", "YYQRS", "JULIAN", "JULDATE", "PDJULG", "PDJULI", "WEEKDATE",
    "WEEKDATX", "WORDDATE", "WORDDATX", "NENGO", "MINGUO", "E8601DA",
    "B8601DA", "NLDATE", "EURDFDD", "EURDFDE", "EURDFMY", "EURDFWDX",
})

#: Date-valued, but equally what a plain integer gets displayed with.
AMBIGUOUS_DATE_FORMATS = frozenset({
    "DAY", "MONTH", "YEAR", "QTR", "QTRR", "WEEKDAY", "WEEKV", "DOWNAME",
    "MONNAME", "JULDAY",
})

DATETIME_FORMATS = frozenset({
    "DATETIME", "DATEAMPM", "MDYAMPM", "DTDATE", "DTMONYY", "DTWKDATX",
    "DTYEAR", "DTYYQC", "B8601DN", "B8601DT", "B8601DX", "B8601DZ", "B8601LX",
    "E8601DN", "E8601DT", "E8601DX", "E8601DZ", "E8601LX", "NLDATM",
})

#: TOD is seconds since midnight, not a datetime -- pandas classifies it as the
#: latter, which is where the disagreement with this table comes from.
TIME_FORMATS = frozenset({
    "TIME", "TIMEAMPM", "TOD", "HHMM", "E8601TM", "B8601TM", "E8601TZ",
    "B8601TZ", "E8601LZ", "B8601LZ", "NLTIME", "NLTIMAP",
})

#: Time-valued, but equally what a plain integer gets displayed with.
AMBIGUOUS_TIME_FORMATS = frozenset({"HOUR", "MMSS"})

# --------------------------------------------------------------------------- #
# Epochs and the ranges Postgres will accept
# --------------------------------------------------------------------------- #

_EPOCH_D = np.datetime64("1960-01-01", "D")
_EPOCH_S = np.datetime64("1960-01-01T00:00:00", "s")
_EPOCH_US = np.datetime64("1960-01-01T00:00:00.000000", "us")

_DAY = np.timedelta64(1, "D")
_SEC = np.timedelta64(1, "s")

#: Postgres date/timestamp accept years 1..9999 (and more, but not in a form
#: worth writing). A SAS value outside that is a corrupt or sentinel date, and
#: is nulled with a count rather than written as something Postgres will refuse
#: halfway through a 500k-row COPY.
MIN_DATE_DAYS = int((np.datetime64("0001-01-01", "D") - _EPOCH_D) / _DAY)
MAX_DATE_DAYS = int((np.datetime64("9999-12-31", "D") - _EPOCH_D) / _DAY)
MIN_DATETIME_SECONDS = int((np.datetime64("0001-01-01T00:00:00", "s") - _EPOCH_S) / _SEC)
MAX_DATETIME_SECONDS = int((np.datetime64("9999-12-31T23:59:59", "s") - _EPOCH_S) / _SEC)
SECONDS_PER_DAY = 86_400

#: Above this a float64 no longer holds consecutive integers, so an integral
#: column beyond it is already approximate in SAS and cannot be given an integer
#: type here without implying a precision the source never had.
INT_EXACT_CAP = float(2**53)

#: Above this, the gap between representable float64 values is wide enough that
#: asking "does this value have two decimal places" stops meaning anything, so
#: scale inference is not attempted and the column stays a binary float.
SCALE_MAGNITUDE_CAP = float(2**45)

_PG_INT_TYPES = (
    (-32_768, 32_767, "smallint"),
    (-2_147_483_648, 2_147_483_647, "integer"),
    (-9_223_372_036_854_775_808, 9_223_372_036_854_775_807, "bigint"),
)

#: Rows held at once when nothing was asked for. Twenty thousand rows of a
#: 200-column extract is about 32 MB of doubles, which keeps the working set
#: small enough to be irrelevant on any machine that can run pandas.
STREAM_CHUNK_ROWS = 20_000

#: The fast reader is a different trade. pyreadstat re-parses from the start of
#: the file for every chunk it is asked for -- row_offset skips rows, it does not
#: seek to them -- so many small chunks turn a linear read into a quadratic one.
#: Chunks are sized from a memory budget instead, which keeps the number of
#: passes in single figures for any extract of the shape this was written for.
FAST_CHUNK_BUDGET_BYTES = 512 << 20
FAST_MIN_CHUNK_ROWS = 50_000

#: Widest numeric a generated DDL will name. Postgres allows far more; a column
#: needing it is a column whose source should be looked at.
MAX_NUMERIC_PRECISION = 38

#: Postgres folds unquoted identifiers to lower case and truncates at 63 bytes.
#: Everything generated here is quoted, so only the length matters.
MAX_IDENTIFIER_BYTES = 63

_RESERVED_START = re.compile(r"^[^a-z_]")
_NON_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_FORMAT_WIDTH = re.compile(r"[0-9.]+$")


def normalise_format(raw: str | bytes | None) -> str:
    """Strip a SAS format down to its name.

    The two readers disagree on this: pandas hands back ``DATETIME`` and
    pyreadstat hands back ``DATETIME28.9`` for the same column. Both normalise
    to ``DATETIME``, which is what the tables above are keyed on.
    """
    if raw is None:
        return ""
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    return _FORMAT_WIDTH.sub("", text.strip().upper().lstrip("$"))


def sql_identifier(name: str, taken: set[str]) -> str:
    """A quoted-safe, unique, lower-case Postgres identifier for a SAS name.

    SAS names are up to 32 bytes and, with VALIDVARNAME=ANY, can contain
    anything at all. Everything generated here is quoted at the point of use, so
    the transformation exists to make names *legible and unique* rather than to
    make them parseable: lower case, non-identifier runs collapsed to one
    underscore, a leading digit prefixed, and a numeric suffix if that collides
    with a name already claimed.
    """
    folded = _NON_IDENTIFIER.sub("_", name.strip().lower()).strip("_")
    if not folded:
        folded = "column"
    if _RESERVED_START.match(folded):
        folded = f"c_{folded}"
    folded = folded.encode("utf-8")[:MAX_IDENTIFIER_BYTES].decode("utf-8", "ignore")

    candidate = folded
    suffix = 2
    while candidate in taken:
        tail = f"_{suffix}"
        head = folded.encode("utf-8")[: MAX_IDENTIFIER_BYTES - len(tail)]
        candidate = head.decode("utf-8", "ignore") + tail
        suffix += 1
    taken.add(candidate)
    return candidate


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclasses.dataclass(slots=True)
class Column:
    """One SAS column, its Postgres projection, and what the data turned out to be.

    The statistics are accumulated over the whole file as it streams, which is
    why the DDL can only be written once the CSV has been.
    """

    index: int
    sas_name: str
    sql_name: str
    label: str
    sas_format: str
    kind: str  # text | numeric | date | datetime | time
    sas_length: int

    n_value: int = 0
    n_null: int = 0

    # Numeric accumulation.
    vmin: float = float("inf")
    vmax: float = float("-inf")
    integral: bool = True
    scale: int | None = 0
    inexact: bool = False
    n_infinite: int = 0

    # Temporal accumulation.
    n_out_of_range: int = 0
    has_fraction: bool = False

    # Text accumulation.
    max_chars: int = 0
    n_nul_stripped: int = 0
    n_undecodable: int = 0

    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def sas_type(self) -> str:
        return "char" if self.kind == "text" else "num"


@dataclasses.dataclass(slots=True)
class Dataset:
    """The result of converting one sas7bdat member."""

    source: pathlib.Path
    member: str
    table: str
    schema: str
    csv_path: pathlib.Path
    ddl_path: pathlib.Path
    columns: list[Column]
    rows: int
    declared_rows: int
    encoding: str
    sas_compression: str
    reader: str
    seconds: float
    csv_bytes: int
    single_column_quoted: bool = False

    @property
    def qualified(self) -> str:
        return f"{quote(self.schema)}.{quote(self.table)}"

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.declared_rows and self.rows != self.declared_rows:
            out.append(
                f"the header declared {self.declared_rows:,} rows and "
                f"{self.rows:,} were read"
            )
        for column in self.columns:
            out.extend(f"{column.sas_name}: {note}" for note in column.notes)
        return out


@dataclasses.dataclass(slots=True)
class Options:
    """Everything the CLI can vary. Defaults are the shipped behaviour."""

    out: pathlib.Path = pathlib.Path("out")
    schema: str = "public"
    table_prefix: str = ""
    reader: str = "stream"
    chunk_rows: int | None = None
    encoding: str | None = None
    fallback_encoding: str = "cp1252"
    on_bad_bytes: str = "replace"
    date_formats: str = "strict"
    treat_as_date: frozenset[str] = frozenset()
    treat_as_datetime: frozenset[str] = frozenset()
    treat_as_time: frozenset[str] = frozenset()
    treat_as_numeric: frozenset[str] = frozenset()
    int_policy: str = "narrow"
    text_type: str = "varchar"
    max_scale: int = 6
    delimiter: str = ","
    overwrite: bool = False
    temp_dir: pathlib.Path | None = None
    quiet: bool = False


# --------------------------------------------------------------------------- #
# Reading the member out of the zip without extracting it
# --------------------------------------------------------------------------- #


class ZipMemberStream(io.RawIOBase):
    """A read-only file over a zip member that never lands on disk.

    The sas7bdat reader seeks to byte zero once and then reads forward in page
    sized blocks, so a decompressing stream is enough. Backward seeks are still
    *implemented* -- by reopening the member and re-reading -- because a silently
    wrong file is worse than a slow one, and ``rewinds`` records whether it ever
    came to that. It stays at zero for every file this has been run against.
    """

    def __init__(self, archive: zipfile.ZipFile, member: str) -> None:
        super().__init__()
        self._archive = archive
        self._member = member
        self._handle = archive.open(member, "r")
        self._pos = 0
        self.rewinds = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read() if size is None or size < 0 else self._handle.read(size)
        self._pos += len(data)
        return data

    def readall(self) -> bytes:
        return self.read(-1)

    def readinto(self, buffer: Any) -> int:  # pragma: no cover - pandas reads directly
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1:
            target = self._pos + offset
        elif whence == 0:
            target = offset
        else:
            raise io.UnsupportedOperation("cannot seek from the end of a zip stream")
        if target == self._pos:
            return self._pos
        if target > self._pos:
            remaining = target - self._pos
            while remaining > 0:
                got = self.read(min(remaining, 1 << 20))
                if not got:
                    break
                remaining -= len(got)
            return self._pos
        self._handle.close()
        self._handle = self._archive.open(self._member, "r")
        self._pos = 0
        self.rewinds += 1
        if target:
            self.seek(target)
        return self._pos

    def close(self) -> None:
        try:
            self._handle.close()
        finally:
            super().close()


def _sas7bdat_members(archive: zipfile.ZipFile) -> list[str]:
    return [
        info.filename
        for info in archive.infolist()
        if not info.is_dir() and info.filename.lower().endswith(".sas7bdat")
    ]


# --------------------------------------------------------------------------- #
# The two readers, presented to the converter as the same thing: a column list
# and an iterator of chunks, each chunk a list of numpy arrays in column order.
# --------------------------------------------------------------------------- #


def _reader_class() -> type:
    try:
        from pandas.io.sas.sas7bdat import SAS7BDATReader
    except ImportError as exc:  # pragma: no cover - a broken pandas install
        raise SystemExit(
            "this pandas does not expose pandas.io.sas.sas7bdat.SAS7BDATReader, "
            "which the streaming reader needs; use --reader fast (pyreadstat) or "
            "install a pandas that does"
        ) from exc
    return SAS7BDATReader


#: Where the SAS encoding code sits in the header. Not shifted by the alignment
#: flags, so it can be read before anything else about the file is known.
_ENCODING_BYTE = 70
_HEADER_PREFIX = 288


def _declared_encoding(prefix: bytes, options: Options) -> tuple[str, bool]:
    """The encoding to read this file with, and whether it is a fallback.

    Sniffed from the header rather than left to ``encoding="infer"``, which is
    how pandas is asked to do the same thing. Two reasons not to use it. On a
    file whose encoding code is not in pandas' table it leaves the string
    ``"infer"`` in place and then fails on the first column name with
    ``LookupError: unknown encoding: infer`` -- three of the nine real files
    this was developed against do exactly that. And recovering from that means
    reopening the reader, which on a zip stream means rewinding it: the header
    has already been read, so the member has to be decompressed from the start
    again. Reading one byte first costs nothing and avoids both.
    """
    if options.encoding:
        return options.encoding, False

    code = prefix[_ENCODING_BYTE] if len(prefix) > _ENCODING_BYTE else 0
    try:
        from pandas.io.sas.sas_constants import encoding_names
    except ImportError:  # pragma: no cover - a pandas that moved the table
        encoding_names = {}

    name = encoding_names.get(code)
    if name:
        try:
            codecs.lookup(name)
        except LookupError:  # pragma: no cover - a name Python cannot resolve
            name = None
    if name:
        return name, False
    return options.fallback_encoding, True


def _open_streaming(source: Any, options: Options, chunk_rows: int, encoding: str) -> Any:
    """Open the sas7bdat reader on a stream positioned at the start."""
    reader_class = _reader_class()
    try:
        return reader_class(
            source,
            convert_dates=False,
            convert_text=False,
            convert_header_text=True,
            encoding=encoding,
            chunksize=chunk_rows,
        )
    except UnicodeDecodeError:
        # A column *name* that will not decode. Rare, and worth recovering from
        # rather than refusing: latin-1 maps every byte to something, so the
        # file opens and the mangled name is visible in the DDL, where it can be
        # dealt with. Costs one rewind, which is the header only.
        if encoding == "latin-1":
            raise
        if hasattr(source, "seek"):
            source.seek(0)
        print(
            f"  a column name does not decode as {encoding}; reading the header "
            f"as latin-1 -- check the generated identifiers",
            file=sys.stderr,
        )
        return reader_class(
            source,
            convert_dates=False,
            convert_text=False,
            convert_header_text=True,
            encoding="latin-1",
            chunksize=chunk_rows,
        )


def _streaming_chunks(reader: Any, chunk_rows: int) -> Iterator[list[np.ndarray]]:
    while True:
        frame = reader.read(chunk_rows)
        if frame.empty:
            return
        # Positional access throughout: SAS does not guarantee unique column
        # names, and label-based access on a duplicate returns a frame.
        yield [frame.iloc[:, j].to_numpy() for j in range(frame.shape[1])]


def _fast_chunk_rows(explicit: int | None, columns: int) -> int:
    """How many rows to ask pyreadstat for at a time.

    Not the streaming default. Every chunk costs a re-parse from the start of
    the file, so small chunks are what turn the fast reader into the slow one.
    """
    if explicit:
        return explicit
    per_row = max(columns, 1) * 8
    return max(FAST_MIN_CHUNK_ROWS, FAST_CHUNK_BUDGET_BYTES // per_row)


def _pyreadstat_chunks(
    path: pathlib.Path, chunk_rows: int, encoding: str | None
) -> Iterator[list[np.ndarray]]:
    import pyreadstat

    offset = 0
    while True:
        frame, _ = pyreadstat.read_sas7bdat(
            str(path),
            disable_datetime_conversion=True,
            row_offset=offset,
            row_limit=chunk_rows,
            **({"encoding": encoding} if encoding else {}),
        )
        if frame.empty:
            return
        yield [frame.iloc[:, j].to_numpy() for j in range(frame.shape[1])]
        offset += len(frame)
        if len(frame) < chunk_rows:
            return


# --------------------------------------------------------------------------- #
# Column planning
# --------------------------------------------------------------------------- #


def _kind_for(sas_format: str, is_text: bool, options: Options) -> str:
    if is_text:
        return "text"
    name = normalise_format(sas_format)
    broad = options.date_formats == "broad"
    if name in DATE_FORMATS or (broad and name in AMBIGUOUS_DATE_FORMATS):
        return "date"
    if name in DATETIME_FORMATS:
        return "datetime"
    if name in TIME_FORMATS or (broad and name in AMBIGUOUS_TIME_FORMATS):
        return "time"
    return "numeric"


def plan_columns(
    names: Sequence[str],
    labels: Sequence[str],
    formats: Sequence[str],
    is_text: Sequence[bool],
    lengths: Sequence[int],
    options: Options,
) -> list[Column]:
    taken: set[str] = set()
    columns: list[Column] = []
    for index, name in enumerate(names):
        sas_format = formats[index] or ""
        kind = _kind_for(sas_format, is_text[index], options)
        upper = name.upper()

        override = None
        if upper in options.treat_as_numeric:
            override = "numeric"
        elif upper in options.treat_as_date:
            override = "date"
        elif upper in options.treat_as_datetime:
            override = "datetime"
        elif upper in options.treat_as_time:
            override = "time"
        if override and not is_text[index]:
            kind = override
        elif override:
            kind = "text"

        column = Column(
            index=index,
            sas_name=name,
            sql_name=sql_identifier(name, taken),
            label=labels[index] or "",
            sas_format=normalise_format(sas_format),
            kind=kind,
            sas_length=int(lengths[index] or 0),
        )
        if (
            kind == "numeric"
            and not is_text[index]
            and column.sas_format in (AMBIGUOUS_DATE_FORMATS | AMBIGUOUS_TIME_FORMATS)
            and not override
        ):
            column.notes.append(
                f"format {column.sas_format} is date-valued in SAS but is also how a "
                f"plain number gets displayed, so it was kept numeric; "
                f"--treat-as-date {name} converts it"
            )
        columns.append(column)
    return columns


def postgres_type(column: Column, options: Options) -> str:
    """The narrowest Postgres type that holds what this column turned out to be."""
    if column.kind == "text":
        if options.text_type == "text" or column.sas_length <= 0:
            return "text"
        return f"varchar({column.sas_length})"
    if column.kind == "date":
        return "date"
    if column.kind == "datetime":
        return "timestamp"
    if column.kind == "time":
        return "time"

    if column.n_value == 0:
        # Nothing but nulls: no evidence for anything narrower, and SAS stores a
        # double, so say so rather than guess.
        return "double precision"
    if column.integral and not column.inexact:
        if options.int_policy == "bigint":
            return "bigint"
        low, high = int(column.vmin), int(column.vmax)
        for lower, upper, name in _PG_INT_TYPES:
            if low >= lower and high <= upper:
                return name
        return f"numeric({MAX_NUMERIC_PRECISION},0)"
    if column.integral and column.inexact:
        return f"numeric({MAX_NUMERIC_PRECISION},0)"
    if column.scale is not None and column.scale > 0:
        magnitude = max(abs(column.vmin), abs(column.vmax))
        digits = len(str(int(magnitude))) if magnitude >= 1 else 1
        precision = min(digits + column.scale + 2, MAX_NUMERIC_PRECISION)
        precision = max(precision, column.scale + 1)
        return f"numeric({precision},{column.scale})"
    return "double precision"


# --------------------------------------------------------------------------- #
# Value conversion
#
# Every temporal conversion is done with numpy datetime64 rather than pandas
# Timestamps, for one reason: datetime64[ns] tops out in 2262 and insurance
# extracts are full of 31DEC9999 end dates. datetime64[s] reaches year 2.9e11.
# --------------------------------------------------------------------------- #


def _finite(values: np.ndarray, column: Column) -> np.ndarray:
    finite = np.isfinite(values)
    infinite = int(np.count_nonzero(np.isinf(values)))
    if infinite:
        column.n_infinite += infinite
    return finite


def _dates_to_text(values: np.ndarray, column: Column) -> np.ndarray:
    finite = _finite(values, column)
    days = np.rint(np.where(finite, values, 0.0))
    usable = finite & (days >= MIN_DATE_DAYS) & (days <= MAX_DATE_DAYS)
    out_of_range = int(np.count_nonzero(finite & ~usable))
    if out_of_range:
        column.n_out_of_range += out_of_range

    stamps = _EPOCH_D + np.where(usable, days, 0.0).astype("int64").astype("timedelta64[D]")
    text = stamps.astype("U10").astype(object)
    text[~usable] = None
    column.n_value += int(np.count_nonzero(usable))
    column.n_null += int(values.size - np.count_nonzero(usable))
    return text


def _datetimes_to_text(values: np.ndarray, column: Column) -> np.ndarray:
    finite = _finite(values, column)
    seconds = np.where(finite, values, 0.0)
    usable = (
        finite
        & (seconds >= MIN_DATETIME_SECONDS)
        & (seconds <= MAX_DATETIME_SECONDS)
    )
    out_of_range = int(np.count_nonzero(finite & ~usable))
    if out_of_range:
        column.n_out_of_range += out_of_range

    kept = np.where(usable, seconds, 0.0)
    fractional = bool(np.any(kept != np.rint(kept)))
    if fractional:
        column.has_fraction = True
        micros = np.rint(kept * 1_000_000.0).astype("int64")
        stamps = _EPOCH_US + micros.astype("timedelta64[us]")
        text = stamps.astype("U26").astype(object)
    else:
        stamps = _EPOCH_S + np.rint(kept).astype("int64").astype("timedelta64[s]")
        text = stamps.astype("U19").astype(object)
    text[~usable] = None
    column.n_value += int(np.count_nonzero(usable))
    column.n_null += int(values.size - np.count_nonzero(usable))
    return text


def _times_to_text(values: np.ndarray, column: Column) -> np.ndarray:
    finite = _finite(values, column)
    seconds = np.where(finite, values, 0.0)
    usable = finite & (seconds >= 0) & (seconds < SECONDS_PER_DAY)
    out_of_range = int(np.count_nonzero(finite & ~usable))
    if out_of_range:
        column.n_out_of_range += out_of_range

    kept = np.where(usable, seconds, 0.0)
    fractional = bool(np.any(kept != np.rint(kept)))
    if fractional:
        column.has_fraction = True
        micros = np.rint(kept * 1_000_000.0).astype("int64")
        stamps = _EPOCH_US + micros.astype("timedelta64[us]")
        rendered = stamps.astype("U26")
    else:
        stamps = _EPOCH_S + np.rint(kept).astype("int64").astype("timedelta64[s]")
        rendered = stamps.astype("U19")
    # The date half is the epoch and carries nothing; a time is what is left
    # after the T. numpy has no vectorised slice across versions worth relying
    # on, and time columns are a small minority of any extract.
    text = np.array([item[11:] for item in rendered.tolist()], dtype=object)
    text[~usable] = None
    column.n_value += int(np.count_nonzero(usable))
    column.n_null += int(values.size - np.count_nonzero(usable))
    return text


def _accumulate_numeric(values: np.ndarray, column: Column, max_scale: int) -> None:
    finite = _finite(values, column)
    present = values[finite]
    column.n_value += int(present.size)
    column.n_null += int(values.size - present.size)
    if present.size == 0:
        return

    low = float(present.min())
    high = float(present.max())
    column.vmin = min(column.vmin, low)
    column.vmax = max(column.vmax, high)

    magnitude = max(abs(low), abs(high))
    if magnitude > INT_EXACT_CAP:
        column.inexact = True

    if column.integral and not np.array_equal(present, np.rint(present)):
        column.integral = False

    if not column.integral and column.scale is not None:
        column.scale = _widen_scale(present, column.scale, max_scale, magnitude)


def _widen_scale(
    present: np.ndarray, scale: int, max_scale: int, magnitude: float
) -> int | None:
    """The smallest decimal scale that still represents every value here.

    ``None`` means no scale up to ``max_scale`` does, which is the signal to
    leave the column a binary float. Above SCALE_MAGNITUDE_CAP the spacing of
    float64 is wider than the digits being asked about, so the question is not
    answered rather than answered wrongly.
    """
    if magnitude > SCALE_MAGNITUDE_CAP:
        return None
    for candidate in range(scale, max_scale + 1):
        if candidate == 0:
            continue
        shifted = present * (10.0**candidate)
        tolerance = np.maximum(1e-6, np.abs(shifted) * 2.0**-40)
        if bool(np.all(np.abs(shifted - np.rint(shifted)) <= tolerance)):
            return candidate
    return None


def _numeric_to_series(values: np.ndarray, integral_so_far: bool) -> Any:
    """Render a numeric column for the CSV.

    A whole number written ``1.0`` will not load into ``bigint``, so integral
    chunks are written through a nullable integer array and come out ``1``. The
    per-chunk decision cannot disagree with the DDL: the column is only given an
    integer type if *every* chunk was integral, and each of those wrote integers.
    """
    finite = np.isfinite(values)
    if integral_so_far and finite.any():
        present = values[finite]
        if (
            np.array_equal(present, np.rint(present))
            and float(np.abs(present).max()) <= INT_EXACT_CAP
        ):
            whole = np.where(finite, values, 0.0).astype("int64")
            return pd.arrays.IntegerArray(whole, ~finite)
    return values


def _text_to_list(
    values: np.ndarray, column: Column, encoding: str, errors: str
) -> list[str | None]:
    """Decode, trim and sanitise one char column.

    Three things happen here that a CSV load depends on. SAS pads char fields to
    their declared width, and an all-blank field is how SAS spells missing, so
    trailing blanks go and an empty result becomes null. A NUL byte cannot be
    stored in a Postgres text value at all, and the two ways of loading disagree
    about it: a server-side ``COPY`` refuses the whole file, while psql's
    ``\\copy`` truncates the value at the NUL and reports success. Since the
    generated load script uses ``\\copy``, leaving one in place would lose the
    rest of that value silently -- so it is dropped here and counted. And
    decoding is done here rather than by the reader so that a single bad byte in
    row 400,000 replaces one character instead of failing the file.
    """
    out: list[str | None] = []
    append = out.append
    longest = column.max_chars
    stripped = 0
    undecodable = 0

    for value in values:
        if type(value) is bytes:
            trimmed = value.rstrip(b"\x00 \t")
            if not trimmed:
                append(None)
                continue
            text = trimmed.decode(encoding, errors)
        elif isinstance(value, str):
            text = value.rstrip("\x00 \t")
            if not text:
                append(None)
                continue
        else:  # NaN, None: missing
            append(None)
            continue

        if "\x00" in text:
            stripped += 1
            text = text.replace("\x00", "")
            if not text:
                append(None)
                continue
        if "\ufffd" in text:
            undecodable += 1
        if len(text) > longest:
            longest = len(text)
        append(text)

    column.max_chars = longest
    column.n_nul_stripped += stripped
    column.n_undecodable += undecodable
    present = sum(1 for item in out if item is not None)
    column.n_value += present
    column.n_null += len(out) - present
    return out


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


class Progress:
    """Says what is happening, at most every two seconds."""

    def __init__(self, label: str, total: int, quiet: bool) -> None:
        self.label = label
        self.total = total
        self.quiet = quiet
        self.started = time.monotonic()
        self._last = 0.0

    def update(self, rows: int, force: bool = False) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if not force and now - self._last < 2.0:
            return
        self._last = now
        elapsed = max(now - self.started, 1e-6)
        rate = rows / elapsed
        if self.total:
            share = f"{100.0 * rows / self.total:5.1f}%"
            remaining = (self.total - rows) / rate if rate > 0 else 0.0
            tail = f" eta {remaining:5.0f}s"
        else:
            share, tail = "     ", ""
        line = f"  {self.label}  {share} {rows:>10,} rows  {rate:>8,.0f} rows/s{tail}"
        print(
            f"{line:<78}",
            end="\n" if force else "\r",
            file=sys.stderr,
            flush=True,
        )


def _convert_member(
    source: pathlib.Path,
    member: str,
    table: str,
    options: Options,
    open_stream: Any,
    spill: pathlib.Path | None,
) -> Dataset:
    started = time.monotonic()

    if spill is not None:
        import pyreadstat

        _, meta = pyreadstat.read_sas7bdat(str(spill), metadataonly=True)
        names = list(meta.column_names)
        labels = [label or "" for label in (meta.column_labels or [None] * len(names))]
        formats = [meta.original_variable_types.get(name, "") or "" for name in names]
        is_text = [
            meta.readstat_variable_types.get(name) == "string" for name in names
        ]
        lengths = [int(meta.variable_storage_width.get(name, 0) or 0) for name in names]
        declared_rows = int(meta.number_rows or 0)
        encoding = meta.file_encoding or ""
        compression = ""
        chunk_rows = _fast_chunk_rows(options.chunk_rows, len(names))
        chunks = _pyreadstat_chunks(spill, chunk_rows, options.encoding)
        reader_name = "fast"
        reader = None
    else:
        chunk_rows = options.chunk_rows or STREAM_CHUNK_ROWS
        with open_stream() as sniffer:
            encoding, fell_back = _declared_encoding(
                sniffer.read(_HEADER_PREFIX), options
            )
        stream = open_stream()
        reader = _open_streaming(stream, options, chunk_rows, encoding)
        names = [str(name) for name in reader.column_names]
        labels = [str(column.label or "") for column in reader.columns]
        formats = [str(column.format or "") for column in reader.columns]
        is_text = [column.ctype == b"s" for column in reader.columns]
        lengths = [int(column.length or 0) for column in reader.columns]
        declared_rows = int(reader.row_count or 0)
        compression = (
            reader.compression.decode("latin-1", "replace")
            if isinstance(reader.compression, bytes)
            else str(reader.compression or "")
        )
        if fell_back and not options.quiet:
            print(
                f"  {member}: the file declares an encoding nobody has a name for; "
                f"reading it as {encoding}",
                file=sys.stderr,
            )
        chunks = _streaming_chunks(reader, chunk_rows)
        reader_name = "stream"

    if not names:
        raise _Skip(f"{member} declares no columns")

    columns = plan_columns(names, labels, formats, is_text, lengths, options)
    csv_path = options.out / f"{table}.csv"
    ddl_path = options.out / f"{table}.sql"
    if csv_path.exists() and not options.overwrite:
        raise _Skip(f"{csv_path.name} already exists; --overwrite to replace it")

    # A single-column CSV is the one shape where a text value can occupy a whole
    # line, and a line holding exactly \. ends a COPY early. Quoting every field
    # puts it out of reach; FORCE_NULL in the load script keeps "" meaning null.
    quote_all = len(columns) == 1
    quoting = csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL

    progress = Progress(member, declared_rows, options.quiet)
    rows = 0
    encoding_for_text = encoding or options.fallback_encoding
    errors = "strict" if options.on_bad_bytes == "strict" else "replace"

    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            for chunk in chunks:
                block: dict[str, Any] = {}
                for column in columns:
                    values = chunk[column.index]
                    if column.kind == "text":
                        block[column.sql_name] = _text_to_list(
                            values, column, encoding_for_text, errors
                        )
                    elif column.kind == "date":
                        block[column.sql_name] = _dates_to_text(values, column)
                    elif column.kind == "datetime":
                        block[column.sql_name] = _datetimes_to_text(values, column)
                    elif column.kind == "time":
                        block[column.sql_name] = _times_to_text(values, column)
                    else:
                        integral = column.integral
                        _accumulate_numeric(values, column, options.max_scale)
                        block[column.sql_name] = _numeric_to_series(values, integral)

                frame = pd.DataFrame(block, copy=False)
                frame.to_csv(
                    handle,
                    index=False,
                    header=rows == 0,
                    na_rep="",
                    sep=options.delimiter,
                    quoting=quoting,
                    lineterminator="\n",
                )
                rows += len(frame)
                progress.update(rows)
        progress.update(rows, force=True)
    except BaseException:
        # A half-written CSV is a file somebody will eventually load.
        csv_path.unlink(missing_ok=True)
        raise
    finally:
        if reader is not None:
            reader.close()

    for column in columns:
        _finalise_notes(column, options)

    dataset = Dataset(
        source=source,
        member=member,
        table=table,
        schema=options.schema,
        csv_path=csv_path,
        ddl_path=ddl_path,
        columns=columns,
        rows=rows,
        declared_rows=declared_rows,
        encoding=encoding_for_text,
        sas_compression=compression,
        reader=reader_name,
        seconds=time.monotonic() - started,
        csv_bytes=csv_path.stat().st_size,
        single_column_quoted=quote_all,
    )
    ddl_path.write_text(render_ddl(dataset, options), encoding="utf-8", newline="\n")
    return dataset


def _finalise_notes(column: Column, options: Options) -> None:
    if column.n_out_of_range:
        column.n_null += 0  # already counted; the note is what matters
        column.notes.append(
            f"{column.n_out_of_range:,} value(s) fell outside the range Postgres "
            f"accepts for {column.kind} and were written as null"
        )
    if column.n_infinite:
        column.notes.append(
            f"{column.n_infinite:,} infinite value(s) were written as null"
        )
    if column.inexact and column.integral:
        column.notes.append(
            "whole numbers beyond 2^53 are already approximate in SAS, so this "
            f"column is numeric({MAX_NUMERIC_PRECISION},0) rather than bigint"
        )
    if column.n_nul_stripped:
        column.notes.append(
            f"{column.n_nul_stripped:,} value(s) contained NUL bytes, which "
            "Postgres cannot store in text; they were removed"
        )
    if column.n_undecodable:
        column.notes.append(
            f"{column.n_undecodable:,} value(s) did not decode cleanly and carry "
            "a replacement character; --on-bad-bytes strict fails instead"
        )
    if column.kind == "text" and column.sas_length and column.max_chars > column.sas_length:
        column.notes.append(
            f"observed {column.max_chars} characters in a column SAS declares as "
            f"{column.sas_length} bytes; the varchar bound follows the data"
        )
    if column.kind == "numeric" and column.scale is None and not column.integral:
        column.notes.append(
            f"no decimal scale up to {options.max_scale} represented every value, "
            "so this column stays double precision"
        )


class _Skip(Exception):
    """A member that cannot be converted, with the reason a person needs."""


# --------------------------------------------------------------------------- #
# Generated SQL
# --------------------------------------------------------------------------- #


def render_ddl(dataset: Dataset, options: Options) -> str:
    lines = [
        f"-- {dataset.table}: {dataset.rows:,} rows, {len(dataset.columns)} columns",
        f"-- converted from {dataset.source.name} -> {dataset.member}",
        f"-- SAS encoding {dataset.encoding}"
        + (f", {dataset.sas_compression} compressed" if dataset.sas_compression else ""),
        "--",
        "-- Types are what the data turned out to be, not what SAS declares: SAS",
        "-- stores every number as an 8-byte float, and this run measured min,",
        "-- max, integrality and decimal scale over every row to narrow that.",
        "",
        f"CREATE TABLE IF NOT EXISTS {dataset.qualified} (",
    ]

    rendered = [(quote(c.sql_name), postgres_type(c, options), c) for c in dataset.columns]
    name_width = max(len(name) for name, _, _ in rendered)
    type_width = max(len(kind) for _, kind, _ in rendered)

    for position, (name, kind, column) in enumerate(rendered):
        kind += "," if position < len(rendered) - 1 else ""
        note = f"{column.sas_name} {column.sas_type}"
        if column.sas_format:
            note += f" {column.sas_format}"
        if column.kind == "text" and column.sas_length:
            note += f" len {column.sas_length}"
        if column.label:
            note += f" -- {column.label}"
        lines.append(
            f"    {name:<{name_width}} {kind:<{type_width + 1}}  -- {note}".rstrip()
        )

    lines.append(");")

    warnings = dataset.warnings
    if warnings:
        lines.extend(["", "-- Worth reading before you trust this table:"])
        lines.extend(f"--   {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def render_load_script(datasets: Sequence[Dataset], options: Options) -> str:
    lines = [
        "-- Load the converted extracts into Azure Database for PostgreSQL.",
        "--",
        "--   psql \"host=SERVER.postgres.database.azure.com port=5432 dbname=DB \\",
        "--         user=USER sslmode=require\" -v ON_ERROR_STOP=1 -f load.sql",
        "--",
        "-- \\copy streams the file from this machine, so the CSVs do not need to be",
        "-- anywhere the server can see. Paths below are absolute; edit them if the",
        "-- files move.",
        "--",
        "-- Running this twice would load everything twice, so each table is",
        "-- checked for rows first and the load stops rather than duplicate them.",
        "-- To load again deliberately: TRUNCATE the tables, or DROP them and let",
        "-- the CREATE statements rebuild them.",
        "",
        "\\set ON_ERROR_STOP on",
        "\\timing on",
        "",
        "-- A bulk load of this size is mostly WAL. Uncomment to trade a crash",
        "-- window for throughput; it is a session setting and needs no privileges.",
        "-- SET synchronous_commit = off;",
        "",
    ]
    for dataset in datasets:
        columns = ", ".join(quote(column.sql_name) for column in dataset.columns)
        force_null = ""
        if dataset.single_column_quoted:
            force_null = f", FORCE_NULL ({columns})"
        name = f"{dataset.schema}.{dataset.table}"
        lines.extend([
            f"-- {dataset.table}: {dataset.rows:,} rows from {dataset.member}",
            "BEGIN;",
            f"\\i '{dataset.ddl_path.resolve().as_posix()}'",
            "DO $$ BEGIN",
            f"  IF EXISTS (SELECT 1 FROM {dataset.qualified} LIMIT 1) THEN",
            f"    RAISE EXCEPTION '{name} already holds rows; TRUNCATE or DROP it "
            "to load again';",
            "  END IF;",
            "END $$;",
            f"\\copy {dataset.qualified} ({columns}) "
            f"FROM '{dataset.csv_path.resolve().as_posix()}' "
            f"WITH (FORMAT csv, HEADER true, NULL ''{force_null}, ENCODING 'UTF8')",
            "COMMIT;",
            f"ANALYZE {dataset.qualified};",
            "",
        ])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Driving it
# --------------------------------------------------------------------------- #


def _tables_from(sources: Sequence[pathlib.Path], options: Options) -> list[
    tuple[pathlib.Path, str, str]
]:
    """Every (archive, member, table) to convert, with collisions resolved."""
    plan: list[tuple[pathlib.Path, str, str]] = []
    claimed: dict[str, tuple[pathlib.Path, str]] = {}
    for source in sources:
        members = (
            _sas7bdat_members(zipfile.ZipFile(source))
            if source.suffix.lower() == ".zip"
            else [source.name]
        )
        for member in members:
            stem = pathlib.PurePosixPath(member).stem
            table = f"{options.table_prefix}{sql_identifier(stem, set())}"
            if table in claimed:
                table = f"{options.table_prefix}{sql_identifier(f'{source.stem} {stem}', set())}"
            claimed[table] = (source, member)
            plan.append((source, member, table))
    return plan


def convert(sources: Sequence[pathlib.Path], options: Options) -> list[Dataset]:
    """Convert every sas7bdat in every source. Returns what was written."""
    options.out.mkdir(parents=True, exist_ok=True)
    datasets: list[Dataset] = []

    for source, member, table in _tables_from(sources, options):
        try:
            if source.suffix.lower() == ".zip":
                with zipfile.ZipFile(source) as archive:
                    datasets.append(_convert_one(source, archive, member, table, options))
            else:
                datasets.append(_convert_one(source, None, member, table, options))
        except _Skip as skip:
            print(f"  skipped: {skip}", file=sys.stderr)
    return datasets


def _convert_one(
    source: pathlib.Path,
    archive: zipfile.ZipFile | None,
    member: str,
    table: str,
    options: Options,
) -> Dataset:
    use_fast = options.reader == "fast" or (
        options.reader == "auto" and _pyreadstat_available()
    )
    if options.reader == "fast" and not _pyreadstat_available():
        raise SystemExit(
            "--reader fast needs pyreadstat, which is not installed; "
            "the default --reader stream needs only pandas"
        )

    if not use_fast:
        # Streams are handed over as a factory rather than as an object: the
        # header is read once to resolve the encoding, and the reader then gets
        # a stream positioned at byte zero instead of one that has to rewind.
        opened: list[Any] = []

        def open_stream() -> Any:
            # Deliberately not a context manager: the caller opens two of these
            # (one to read the header, one to read the data) and the `finally`
            # below closes every one that was handed out.
            handle = (
                open(source, "rb")  # noqa: SIM115
                if archive is None
                else ZipMemberStream(archive, member)
            )
            opened.append(handle)
            return handle

        try:
            dataset = _convert_member(source, member, table, options, open_stream, None)
        finally:
            for handle in opened:
                handle.close()
        rewinds = sum(getattr(handle, "rewinds", 0) for handle in opened)
        if rewinds:
            print(
                f"  note: {member} needed {rewinds} rewind(s) of the zip stream, "
                "which means re-decompressing it from the start; --reader fast "
                "reads a spilled copy instead",
                file=sys.stderr,
            )
        return dataset

    if archive is None:
        return _convert_member(source, member, table, options, None, source)

    scratch = options.temp_dir or pathlib.Path(tempfile.gettempdir())
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=scratch, suffix=".sas7bdat", delete=False
    ) as spill:
        spill_path = pathlib.Path(spill.name)
    try:
        with archive.open(member, "r") as inside, open(spill_path, "wb") as out:
            shutil.copyfileobj(inside, out, length=8 << 20)
        return _convert_member(source, member, table, options, None, spill_path)
    finally:
        spill_path.unlink(missing_ok=True)


def _pyreadstat_available() -> bool:
    try:
        import pyreadstat  # noqa: F401
    except ImportError:
        return False
    return True


def describe(sources: Sequence[pathlib.Path], options: Options) -> None:
    """Print what is in the archives without converting anything.

    Reads the header and the metadata pages and stops, so it is as quick on a
    5 GB archive as on a small one.
    """
    for source, member, table in _tables_from(sources, options):
        with contextlib.ExitStack() as stack:
            archive = (
                stack.enter_context(zipfile.ZipFile(source))
                if source.suffix.lower() == ".zip"
                else None
            )

            def fresh(
                archive: zipfile.ZipFile | None = archive,
                source: pathlib.Path = source,
                member: str = member,
            ) -> Any:
                handle = (
                    open(source, "rb")  # noqa: SIM115 - closed by the ExitStack
                    if archive is None
                    else ZipMemberStream(archive, member)
                )
                return stack.enter_context(handle)

            encoding, _ = _declared_encoding(fresh().read(_HEADER_PREFIX), options)
            reader = stack.enter_context(
                contextlib.closing(_open_streaming(fresh(), options, 1, encoding))
            )
            columns = plan_columns(
                [str(name) for name in reader.column_names],
                [str(c.label or "") for c in reader.columns],
                [str(c.format or "") for c in reader.columns],
                [c.ctype == b"s" for c in reader.columns],
                [int(c.length or 0) for c in reader.columns],
                options,
            )
            compression = (
                reader.compression.decode("latin-1", "replace")
                if isinstance(reader.compression, bytes)
                else ""
            )
            print(
                f"\n{source.name} -> {member}\n"
                f"  table {options.schema}.{table}, {reader.row_count:,} rows, "
                f"{len(columns)} columns, encoding {encoding}"
                + (f", {compression} compressed" if compression else "")
            )
            for column in columns:
                print(
                    f"    {column.sas_name:<32} {column.sas_type:<5} "
                    f"{column.sas_format:<10} -> {column.kind}"
                )


def _summarise(datasets: Sequence[Dataset], options: Options) -> None:
    print("")
    for dataset in datasets:
        rate = dataset.rows / dataset.seconds if dataset.seconds else 0.0
        print(
            f"  {dataset.table}: {dataset.rows:,} rows x {len(dataset.columns)} cols "
            f"-> {dataset.csv_path.name} "
            f"({dataset.csv_bytes / 1e6:,.1f} MB, {dataset.seconds:,.1f}s, "
            f"{rate:,.0f} rows/s, {dataset.reader})"
        )
        kinds: dict[str, int] = {}
        for column in dataset.columns:
            kind = postgres_type(column, options)
            kinds[kind] = kinds.get(kind, 0) + 1
        print(
            "    types: "
            + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        )
        for warning in dataset.warnings:
            print(f"    ! {warning}")


def _warn_about_a_replaced_load_script(load: pathlib.Path, datasets: Sequence[Dataset]) -> None:
    """Say when an earlier run's load script is about to be lost.

    The CSVs of an earlier run are protected -- writing over one needs
    ``--overwrite`` -- but ``load.sql`` covers whatever the run that wrote it
    converted, so a second run into the same directory replaces a script that
    loaded other tables. The CSVs and their DDL are all still there, so nothing
    is unrecoverable; what would be lost is the knowledge that they need
    loading. Convert every archive in one run and this never arises.
    """
    if not load.exists():
        return
    written = {dataset.qualified for dataset in datasets}
    previous = {
        line.split()[1]
        for line in load.read_text(encoding="utf-8").splitlines()
        if line.startswith("\\copy")
    }
    orphaned = sorted(previous - written)
    if orphaned:
        print(
            f"  note: replacing a load.sql that also loaded {', '.join(orphaned)}; "
            f"convert every archive in one run to get one script for all of them",
            file=sys.stderr,
        )


def _names(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.sas2csv",
        description="Convert zipped sas7bdat extracts to CSV for Azure Postgres.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The zip is read as a stream: nothing is extracted, and no scratch\n"
            "space is needed. Writes one CSV and one CREATE TABLE per dataset,\n"
            "plus a load.sql that runs them through psql.\n"
        ),
    )
    parser.add_argument(
        "sources", nargs="+", type=pathlib.Path,
        help="zip archives (or bare .sas7bdat files) to convert",
    )
    parser.add_argument("-o", "--out", type=pathlib.Path, default=pathlib.Path("out"),
                        help="output directory (default: ./out)")
    parser.add_argument("--schema", default="public", help="target schema (default: public)")
    parser.add_argument("--table-prefix", default="", help="prefix for generated table names")
    parser.add_argument("--reader", choices=("stream", "fast", "auto"), default="stream",
                        help="stream reads out of the zip and extracts nothing (default); "
                             "fast spills the member to a temporary file and uses "
                             "pyreadstat; auto is fast when pyreadstat is installed")
    parser.add_argument("--chunk-rows", type=int,
                        help=f"rows held in memory at once (default: "
                             f"{STREAM_CHUNK_ROWS} streaming; --reader fast sizes "
                             f"chunks from a memory budget instead)")
    parser.add_argument("--encoding",
                        help="override the encoding declared in the file")
    parser.add_argument("--fallback-encoding", default="cp1252",
                        help="encoding used when the file declares one that is not "
                             "recognised (default: cp1252)")
    parser.add_argument("--on-bad-bytes", choices=("replace", "strict"), default="replace",
                        help="replace undecodable bytes and count them (default), or fail")
    parser.add_argument("--date-formats", choices=("strict", "broad"), default="strict",
                        help="strict converts only unambiguous date formats (default); "
                             "broad also converts YEAR, MONTH, DAY, QTR and friends")
    parser.add_argument("--treat-as-date", help="comma-separated SAS columns to read as dates")
    parser.add_argument("--treat-as-datetime", help="comma-separated columns to read as timestamps")
    parser.add_argument("--treat-as-time", help="comma-separated columns to read as times")
    parser.add_argument("--treat-as-numeric", help="comma-separated columns to leave numeric")
    parser.add_argument("--int-policy", choices=("narrow", "bigint"), default="narrow",
                        help="narrow picks smallint/integer/bigint from the observed "
                             "range (default); bigint gives every whole-number column "
                             "room to grow")
    parser.add_argument("--text-type", choices=("varchar", "text"), default="varchar",
                        help="varchar(n) from the SAS declared width (default), or text")
    parser.add_argument("--max-scale", type=int, default=6,
                        help="widest decimal scale numeric(p,s) inference will try "
                             "(default: 6)")
    parser.add_argument("--delimiter", default=",", help="CSV delimiter (default: ,)")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace CSVs left by an earlier run")
    parser.add_argument("--temp-dir", type=pathlib.Path,
                        help="where --reader fast spills the member")
    parser.add_argument("--list", action="store_true",
                        help="print each dataset's columns and types, convert nothing")
    parser.add_argument("--quiet", action="store_true", help="no progress output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # A Windows console is cp1252 and column labels are not, and a converter
    # that dies formatting its own progress line is a poor showing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(errors="replace")

    arguments = build_parser().parse_args(argv)
    options = Options(
        out=arguments.out,
        schema=arguments.schema,
        table_prefix=arguments.table_prefix,
        reader=arguments.reader,
        chunk_rows=max(1, arguments.chunk_rows) if arguments.chunk_rows else None,
        encoding=arguments.encoding,
        fallback_encoding=arguments.fallback_encoding,
        on_bad_bytes=arguments.on_bad_bytes,
        date_formats=arguments.date_formats,
        treat_as_date=_names(arguments.treat_as_date),
        treat_as_datetime=_names(arguments.treat_as_datetime),
        treat_as_time=_names(arguments.treat_as_time),
        treat_as_numeric=_names(arguments.treat_as_numeric),
        int_policy=arguments.int_policy,
        text_type=arguments.text_type,
        max_scale=max(0, arguments.max_scale),
        delimiter=arguments.delimiter,
        overwrite=arguments.overwrite,
        temp_dir=arguments.temp_dir,
        quiet=arguments.quiet,
    )

    missing = [str(source) for source in arguments.sources if not source.exists()]
    if missing:
        print(f"STOPPED: no such file: {', '.join(missing)}", file=sys.stderr)
        return 1

    if arguments.list:
        describe(arguments.sources, options)
        return 0

    datasets = convert(arguments.sources, options)
    if not datasets:
        print("STOPPED: nothing was converted", file=sys.stderr)
        return 1

    load = options.out / "load.sql"
    _warn_about_a_replaced_load_script(load, datasets)
    load.write_text(render_load_script(datasets, options), encoding="utf-8", newline="\n")
    _summarise(datasets, options)
    print(f"\n  wrote {len(datasets)} dataset(s) to {options.out.resolve()}")
    print(f"  load them with: psql \"...\" -v ON_ERROR_STOP=1 -f {load.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
