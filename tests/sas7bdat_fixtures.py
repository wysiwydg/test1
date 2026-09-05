"""Write sas7bdat files, so the converter can be tested against real bytes.

There is no SAS on this machine and no Python library writes sas7bdat --
pyreadstat reads it and writes only sav, dta, por and xport -- so testing
``scripts.sas2csv`` against anything but a mock means generating the format.
This does that: uncompressed, 64-bit, little-endian, which is what a modern
Windows or Linux SAS session produces.

Enough of the format to be read back by *two independent implementations*. The
tests assert pandas and pyreadstat agree on every fixture written here, which is
what makes a fixture generator trustworthy: a file only my own reader accepts
would prove nothing about the files the converter will meet in production.

The layout, for anyone who has to change this:

* A 288-byte header prefix (magic, alignment flags, endianness, encoding code)
  padded out to ``page_size``. ``0x33`` at byte 32 is what marks the file
  64-bit, and *anything else* at byte 35 leaves the header field offsets
  unshifted, which is the simpler of the two conventions real files use.
* One metadata page (type ``0x0000``) carrying six kinds of subheader, in the
  order a reader needs them: row size, column size, column text, column names,
  column attributes, then one format-and-label subheader per column.
* Data pages (type ``0x0100``), rows starting at byte 40 -- the 32-byte page
  preamble plus the 8-byte page header -- packed at their declared offsets.

Compressed files are deliberately not generated. RLE and RDC decompression lives
in pandas and pyreadstat, not in the converter, and the converter was checked
against genuinely compressed files from the pandas test corpus during
development instead of against a re-implementation of the compressor here.
"""

from __future__ import annotations

import dataclasses
import pathlib
import struct

import numpy as np

MAGIC = (
    b"\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\xc2\xea\x81\x60"
    b"\xb3\x14\x11\xcf\xbd\x92\x08\x00"
    b"\x09\xc7\x31\x8c\x18\x1f\x10\x11"
)

#: SAS encoding byte -> the codec pandas maps it to. Only the ones used here.
ENCODING_CODES = {"utf-8": 20, "latin1": 29, "cp1252": 62}

PAGE_PREAMBLE = 32  # bytes before the page header, in the 64-bit layout
PAGE_HEADER = 8  # page type, block count, subheader count
ROW_START = PAGE_PREAMBLE + PAGE_HEADER
POINTER_LENGTH = 24  # subheader pointer, 64-bit layout
INT = 8

PAGE_META = 0x0000
PAGE_DATA = 0x0100

SIG_ROW_SIZE = b"\xf7\xf7\xf7\xf7\x00\x00\x00\x00"
SIG_COLUMN_SIZE = b"\xf6\xf6\xf6\xf6\x00\x00\x00\x00"
SIG_COLUMN_TEXT = b"\xfd\xff\xff\xff\xff\xff\xff\xff"
SIG_COLUMN_NAME = b"\xff\xff\xff\xff\xff\xff\xff\xff"
SIG_COLUMN_ATTRS = b"\xfc\xff\xff\xff\xff\xff\xff\xff"
SIG_FORMAT = b"\xfe\xfb\xff\xff\xff\xff\xff\xff"

ROW_SIZE_LENGTH = 808  # must reach the lcs/lcp fields at +682 and +706
COLUMN_SIZE_LENGTH = 24
FORMAT_LENGTH = 64  # must reach the label length field at +56

#: Where strings start inside a column-text block. The first 2 bytes are the
#: block's own size and the area behind it is where a reader looks for the
#: compression literal and the creating procedure, so strings stay clear of it.
TEXT_DATA_START = 28


@dataclasses.dataclass(frozen=True, slots=True)
class Spec:
    """One column to write."""

    name: str
    numeric: bool = True
    width: int = 8
    sas_format: str = ""
    label: str = ""
    #: The display width SAS shows the format at. ReadStat appends it to the
    #: format name (DATE -> DATE9); pandas drops it. Both reach the converter,
    #: which is the point of carrying it here.
    format_width: int = 0

    @property
    def storage(self) -> int:
        return 8 if self.numeric else self.width


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


class _TextBlock:
    """The one column-text subheader: names, formats and labels, interned."""

    def __init__(self) -> None:
        self._data = bytearray(b" " * TEXT_DATA_START)
        self._seen: dict[bytes, tuple[int, int]] = {}

    def intern(self, text: str, encoding: str) -> tuple[int, int]:
        """Return (offset, length) of ``text`` inside the block."""
        if not text:
            return (0, 0)
        raw = text.encode(encoding)
        if raw not in self._seen:
            self._seen[raw] = (len(self._data), len(raw))
            self._data.extend(raw)
        return self._seen[raw]

    def subheader(self) -> bytes:
        """The subheader both readers agree on.

        Both slice the text blob from just past the signature, so interned
        offsets are relative to the two-byte size field -- which is why intern()
        started its cursor past them. The size itself has to satisfy two
        readings at once: pandas takes it as the number of bytes to slice, and
        ReadStat requires it to equal ``length - 20``. Padding the subheader 12
        bytes beyond the blob makes both true of the same number.
        """
        blob = bytearray(self._data)
        if len(blob) % 8:
            blob.extend(b" " * (8 - len(blob) % 8))
        blob[0:2] = _u16(len(blob))
        return SIG_COLUMN_TEXT + bytes(blob) + b" " * 12


def _row_size_subheader(row_length: int, row_count: int, column_count: int) -> bytes:
    body = bytearray(b"\x00" * ROW_SIZE_LENGTH)
    body[0:INT] = SIG_ROW_SIZE
    body[5 * INT : 6 * INT] = _u64(row_length)
    body[6 * INT : 7 * INT] = _u64(row_count)
    body[9 * INT : 10 * INT] = _u64(column_count)  # col_count_p1
    body[10 * INT : 11 * INT] = _u64(0)  # col_count_p2
    body[15 * INT : 16 * INT] = _u64(0)  # rows on a mix page; there are none
    body[682:684] = _u16(0)  # lcs
    body[706:708] = _u16(0)  # lcp
    return bytes(body)


def _column_size_subheader(column_count: int) -> bytes:
    body = bytearray(b"\x00" * COLUMN_SIZE_LENGTH)
    body[0:INT] = SIG_COLUMN_SIZE
    body[INT : 2 * INT] = _u64(column_count)
    return bytes(body)


def _column_name_subheader(pointers: list[tuple[int, int, int]]) -> bytes:
    length = 28 + 8 * len(pointers)
    body = bytearray(b"\x00" * length)
    body[0:INT] = SIG_COLUMN_NAME
    body[INT : INT + 2] = _u16(length - 20)  # ReadStat validates this
    for index, (text_index, offset, size) in enumerate(pointers):
        at = 16 + 8 * index
        body[at : at + 2] = _u16(text_index)
        body[at + 2 : at + 4] = _u16(offset)
        body[at + 4 : at + 6] = _u16(size)
    return bytes(body)


def _column_attrs_subheader(specs: list[Spec], offsets: list[int]) -> bytes:
    length = 28 + 16 * len(specs)
    body = bytearray(b"\x00" * length)
    body[0:INT] = SIG_COLUMN_ATTRS
    body[INT : INT + 2] = _u16(length - 20)  # ReadStat validates this
    for index, spec in enumerate(specs):
        at = 16 + 16 * index
        body[at : at + 8] = _u64(offsets[index])
        body[at + 8 : at + 12] = _u32(spec.storage)
        body[at + 14] = 1 if spec.numeric else 2
    return bytes(body)


def _format_subheader(
    sas_format: tuple[int, int], label: tuple[int, int], format_width: int
) -> bytes:
    body = bytearray(b"\x00" * FORMAT_LENGTH)
    body[0:INT] = SIG_FORMAT
    body[24:26] = _u16(format_width)
    body[46:48] = _u16(0)  # format text block index
    body[48:50] = _u16(sas_format[0])
    body[50:52] = _u16(sas_format[1])
    body[52:54] = _u16(0)  # label text block index
    body[54:56] = _u16(label[0])
    body[56:58] = _u16(label[1])
    return bytes(body)


#: Signatures real files flag as "type 1" in their subheader pointers. Neither
#: reader checks it, but a fixture that looks like a real file is one fewer
#: difference to think about when one of them changes its mind.
_POINTER_TYPE_1 = (SIG_COLUMN_TEXT, SIG_COLUMN_NAME, SIG_COLUMN_ATTRS, SIG_FORMAT)


def _page(page_size: int, page_type: int, subheaders: list[bytes], rows: bytes) -> bytes:
    """Assemble one page: header, subheader pointers, subheaders from the end."""
    page = bytearray(b"\x00" * page_size)
    page[PAGE_PREAMBLE : PAGE_PREAMBLE + 2] = _u16(page_type)
    count = len(subheaders)
    page[PAGE_PREAMBLE + 2 : PAGE_PREAMBLE + 4] = _u16(count or len(rows))
    page[PAGE_PREAMBLE + 4 : PAGE_PREAMBLE + 6] = _u16(count)

    # Subheaders are laid down from the end of the page backwards, which is what
    # real files do and what keeps the pointer array free to grow forwards.
    end = page_size
    for index, body in enumerate(subheaders):
        end -= len(body)
        page[end : end + len(body)] = body
        pointer = ROW_START + POINTER_LENGTH * index
        page[pointer : pointer + 8] = _u64(end)
        page[pointer + 8 : pointer + 16] = _u64(len(body))
        page[pointer + 16] = 0  # not compressed, not truncated
        page[pointer + 17] = 1 if body[:INT] in _POINTER_TYPE_1 else 0
    if rows:
        page[ROW_START : ROW_START + len(rows)] = rows
    return bytes(page)


def _data_page(page_size: int, block: np.ndarray) -> bytes:
    page = bytearray(b"\x00" * page_size)
    page[PAGE_PREAMBLE : PAGE_PREAMBLE + 2] = _u16(PAGE_DATA)
    page[PAGE_PREAMBLE + 2 : PAGE_PREAMBLE + 4] = _u16(len(block))
    page[PAGE_PREAMBLE + 4 : PAGE_PREAMBLE + 6] = _u16(0)
    raw = block.tobytes()
    page[ROW_START : ROW_START + len(raw)] = raw
    return bytes(page)


def _pack_rows(specs: list[Spec], data: dict[str, np.ndarray], offsets: list[int],
               row_length: int, encoding: str) -> np.ndarray:
    """Column-major values into a (rows, row_length) byte matrix.

    Vectorised because the point of one of these fixtures is half a million
    rows, and packing 100 million values a call to struct.pack at a time is not
    a test, it is a wait. Called one page-worth at a time, so the whole extract
    is never in memory at once.
    """
    n_rows = len(next(iter(data.values()))) if data else 0
    matrix = np.full((n_rows, row_length), 0x20, dtype=np.uint8)
    for index, spec in enumerate(specs):
        values = data[spec.name]
        start = offsets[index]
        if spec.numeric:
            doubles = np.asarray(values, dtype="<f8")
            matrix[:, start : start + 8] = doubles.view(np.uint8).reshape(n_rows, 8)
        else:
            # Padded here rather than by numpy's S dtype, which pads with NUL:
            # SAS pads char fields with blanks, and a fixture that quietly turned
            # every interior NUL into a blank could not test NUL handling.
            joined = b"".join(
                ("" if value is None else str(value))
                .encode(encoding)[: spec.width]
                .ljust(spec.width, b" ")
                for value in values
            )
            matrix[:, start : start + spec.width] = np.frombuffer(
                joined, dtype=np.uint8
            ).reshape(n_rows, spec.width)
    return matrix


def write_sas7bdat(
    path: pathlib.Path,
    specs: list[Spec],
    data: dict[str, np.ndarray],
    *,
    encoding: str = "utf-8",
    dataset: str = "FIXTURE",
    page_size: int | None = None,
    declared_rows: int | None = None,
) -> pathlib.Path:
    """Write one uncompressed 64-bit sas7bdat and return its path."""
    if encoding not in ENCODING_CODES:
        raise ValueError(f"no SAS encoding code known for {encoding!r}")
    n_rows = len(next(iter(data.values()))) if data else 0

    offsets: list[int] = []
    cursor = 0
    for spec in specs:
        offsets.append(cursor)
        cursor += spec.storage
    row_length = cursor or 1

    text = _TextBlock()
    name_pointers = [(0, *text.intern(spec.name, encoding)) for spec in specs]
    formats = [text.intern(spec.sas_format, encoding) for spec in specs]
    labels = [text.intern(spec.label, encoding) for spec in specs]

    subheaders = [
        _row_size_subheader(row_length, declared_rows or n_rows, len(specs)),
        _column_size_subheader(len(specs)),
        text.subheader(),
        _column_name_subheader(name_pointers),
        _column_attrs_subheader(specs, offsets),
        *[
            _format_subheader(formats[i], labels[i], specs[i].format_width)
            for i in range(len(specs))
        ],
    ]

    needed = ROW_START + POINTER_LENGTH * len(subheaders) + sum(map(len, subheaders))
    minimum = max(needed, ROW_START + row_length, 8192)
    if page_size is None:
        page_size = 1 << max(13, (minimum - 1).bit_length())
    elif page_size < minimum:
        raise ValueError(f"page_size {page_size} cannot hold {minimum} bytes")

    rows_per_page = (page_size - ROW_START) // row_length
    starts = list(range(0, n_rows, rows_per_page))
    page_count = 1 + len(starts)

    header = bytearray(b"\x00" * page_size)
    header[0 : len(MAGIC)] = MAGIC
    header[32] = 0x33  # 64-bit
    header[35] = 0x22  # header field offsets unshifted
    header[37] = 0x01  # little-endian
    header[39] = ord("1")  # unix platform code; readers only report it
    header[70] = ENCODING_CODES[encoding]
    # ReadStat refuses a file without this literal; pandas never looks at it.
    header[84:92] = b"SAS FILE"
    header[92 : 92 + len(dataset)] = dataset.encode("latin-1")[:64]
    header[156:164] = b"DATA    "
    # Timestamps are seconds since 1960-01-01; any plausible value will do.
    header[164:172] = struct.pack("<d", 1_700_000_000.0)
    header[172:180] = struct.pack("<d", 1_700_000_000.0)
    header[196:200] = _u32(page_size)
    header[200:204] = _u32(page_size)
    header[204:208] = _u32(page_count)
    # Release and host sit at 220, not the 216 pandas names -- and ReadStat
    # insists the release parses as "%c.%04dM%1d", so it cannot be left blank.
    header[220:228] = b"9.0401M7"
    header[228:244] = b"X64_DSRV".ljust(16, b"\x00")

    # Written a page at a time. Half a million rows of a 200-column extract is
    # 800 MB, and a fixture generator that needs all of it in memory (twice,
    # once packed and once joined) is one that cannot generate the interesting
    # case.
    with open(path, "wb") as out:
        out.write(bytes(header))
        out.write(_page(page_size, PAGE_META, subheaders, b""))
        for start in starts:
            window = {
                spec.name: data[spec.name][start : start + rows_per_page]
                for spec in specs
            }
            out.write(
                _data_page(
                    page_size,
                    _pack_rows(specs, window, offsets, row_length, encoding),
                )
            )
    return path
