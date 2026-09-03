"""Tests for the zipped-sas7bdat to CSV converter.

Against real sas7bdat bytes, not a mock reader. ``sas7bdat_fixtures`` writes the
format so these can cover the things that actually go wrong on a 500k-row
extract -- a whole number written ``1.0`` that then will not load into
``bigint``, a 31DEC9999 sentinel date, a NUL byte inside a char field, an
encoding pandas has never heard of -- and the fixtures are checked against a
second, independent reader so that a bug in the generator cannot pass as a
converted file.

The last test loads the generated CSV into a real PostgreSQL with the generated
DDL and the generated ``\\copy`` script, because everything else in here only
proves the converter is self-consistent. It skips loudly when there is no
database to reach.
"""

from __future__ import annotations

import csv
import os
import pathlib
import shutil
import subprocess
import zipfile

import numpy as np
import pytest
from sas7bdat_fixtures import Spec, write_sas7bdat

from scripts.sas2csv import (
    Options,
    convert,
    normalise_format,
    postgres_type,
    render_load_script,
    sql_identifier,
)

#: 31DEC9999, the end date every insurance extract is full of. Two days out and
#: it is a different date, so it is computed rather than typed.
SENTINEL_DATE = 2_936_549.0


def zipped(tmp_path: pathlib.Path, name: str, specs, data, **kwargs) -> pathlib.Path:
    """One sas7bdat, in one zip, as the converter will meet it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    member = write_sas7bdat(tmp_path / f"{name}.sas7bdat", specs, data, **kwargs)
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.write(member, member.name)
    return archive


def run(tmp_path: pathlib.Path, archive: pathlib.Path, **overrides):
    options = Options(out=tmp_path / "out", quiet=True, **overrides)
    datasets = convert([archive], options)
    return datasets, options


def rows_of(path: pathlib.Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def column(dataset, sas_name):
    return next(c for c in dataset.columns if c.sas_name == sas_name)


def types_of(dataset, options) -> dict[str, str]:
    return {c.sas_name: postgres_type(c, options) for c in dataset.columns}


# --------------------------------------------------------------------------- #
# The fixture generator itself
# --------------------------------------------------------------------------- #


def test_the_fixtures_are_readable_by_a_second_implementation(tmp_path) -> None:
    """A file only pandas accepts would prove nothing about real extracts."""
    pyreadstat = pytest.importorskip("pyreadstat")
    specs = [
        Spec("N", sas_format="BEST"),
        Spec("D", sas_format="DATE", format_width=9),
        Spec("S", numeric=False, width=10, sas_format="$", label="A label"),
    ]
    data = {
        "N": np.array([1.0, 2.5, np.nan]),
        "D": np.array([0.0, 23_011.0, SENTINEL_DATE]),
        "S": np.array(["one", "two", ""], dtype=object),
    }
    path = write_sas7bdat(tmp_path / "x.sas7bdat", specs, data)

    frame, meta = pyreadstat.read_sas7bdat(str(path), disable_datetime_conversion=True)
    assert meta.number_rows == 3
    assert meta.column_names == ["N", "D", "S"]
    assert meta.column_labels[2] == "A label"
    # ReadStat appends the display width to the format name; the converter
    # normalises both spellings to the same thing.
    assert meta.original_variable_types["D"] == "DATE9"
    assert normalise_format(meta.original_variable_types["D"]) == "DATE"
    assert frame["D"].tolist() == [0.0, 23_011.0, SENTINEL_DATE]
    assert frame["N"].tolist()[:2] == [1.0, 2.5]


# --------------------------------------------------------------------------- #
# Streaming out of the zip
# --------------------------------------------------------------------------- #


def test_streaming_and_extracting_agree(tmp_path) -> None:
    """The whole no-extraction premise: same bytes out, and no re-reads.

    If the reader ever seeks backwards, a zip stream has to be reopened and
    re-decompressed from the start, which on a 300 MB archive would be
    catastrophic rather than merely slow. It does not, and this is what says so.
    """
    specs = [Spec(f"C{i:02d}", sas_format="BEST") for i in range(40)]
    specs.append(Spec("TXT", numeric=False, width=24, sas_format="$"))
    rng = np.random.default_rng(3)
    data = {spec.name: rng.uniform(-1e6, 1e6, 2_000) for spec in specs[:-1]}
    data["TXT"] = np.array([f"row {i}" for i in range(2_000)], dtype=object)

    member = write_sas7bdat(tmp_path / "wide.sas7bdat", specs, data)
    archive = tmp_path / "wide.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.write(member, member.name)

    from_zip, _ = run(tmp_path, archive, chunk_rows=256)
    from_disk, _ = run(tmp_path / "loose", member, chunk_rows=256)

    assert from_zip[0].rows == 2_000
    assert (
        from_zip[0].csv_path.read_bytes()
        == from_disk[0].csv_path.read_bytes()
    )


def rewinds_during(tmp_path: pathlib.Path, archive: pathlib.Path, **overrides) -> int:
    """Convert, and report how often the zip had to be re-decompressed."""
    from scripts.sas2csv import ZipMemberStream

    seen: list[ZipMemberStream] = []
    original = ZipMemberStream.__init__

    def spy(self, archive, member):
        original(self, archive, member)
        seen.append(self)

    ZipMemberStream.__init__ = spy
    try:
        datasets, _ = run(tmp_path, archive, **overrides)
    finally:
        ZipMemberStream.__init__ = original

    assert seen, "no zip member was streamed"
    assert datasets, "nothing was converted"
    return sum(stream.rewinds for stream in seen)


def test_the_zip_stream_is_never_rewound(tmp_path) -> None:
    specs = [Spec("N", sas_format="BEST"), Spec("T", numeric=False, width=8)]
    data = {"N": np.arange(5_000, dtype=float), "T": np.array(["x"] * 5_000, dtype=object)}
    assert rewinds_during(tmp_path, zipped(tmp_path, "r", specs, data), chunk_rows=64) == 0


@pytest.mark.parametrize("reader", ["stream", "fast"])
def test_both_readers_write_the_same_csv(tmp_path, reader) -> None:
    if reader == "fast":
        pytest.importorskip("pyreadstat")
    specs = [
        Spec("ID", sas_format="BEST"),
        Spec("AMOUNT", sas_format="DOLLAR", format_width=12),
        Spec("WHEN", sas_format="DATETIME", format_width=20),
        Spec("WHO", numeric=False, width=20, sas_format="$"),
    ]
    data = {
        "ID": np.arange(1, 101, dtype=float),
        "AMOUNT": np.round(np.linspace(0.01, 999.99, 100), 2),
        "WHEN": np.linspace(0, 2e9, 100).round(),
        "WHO": np.array(["  leading", "trailing  ", "Ünicode", ""] * 25, dtype=object),
    }
    archive = zipped(tmp_path, "both", specs, data)
    datasets, _ = run(tmp_path / reader, archive, reader=reader, chunk_rows=32)
    assert datasets[0].reader == reader
    assert datasets[0].rows == 100
    (tmp_path / f"{reader}.csv").write_bytes(datasets[0].csv_path.read_bytes())


def test_the_two_readers_are_byte_identical(tmp_path) -> None:
    pytest.importorskip("pyreadstat")
    specs = [
        Spec("ID", sas_format="BEST"),
        Spec("AMOUNT", sas_format="DOLLAR"),
        Spec("WHO", numeric=False, width=20, sas_format="$"),
    ]
    data = {
        "ID": np.arange(1, 51, dtype=float),
        "AMOUNT": np.round(np.linspace(0.01, 99.99, 50), 2),
        "WHO": np.array(["  leading", "trailing  ", "Ünicode", "", "x"] * 10, dtype=object),
    }
    archive = zipped(tmp_path, "cmp", specs, data)
    streamed, _ = run(tmp_path / "a", archive, reader="stream")
    fast, _ = run(tmp_path / "b", archive, reader="fast")
    assert streamed[0].csv_path.read_bytes() == fast[0].csv_path.read_bytes()


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


def test_numeric_types_follow_the_data_not_the_declaration(tmp_path) -> None:
    """SAS says "8-byte float" for all of these. The database should not."""
    specs = [
        Spec("TINY", sas_format="BEST"),
        Spec("MEDIUM", sas_format="BEST"),
        Spec("LARGE", sas_format="BEST"),
        Spec("MONEY", sas_format="DOLLAR", format_width=12),
        Spec("RATE", sas_format="BEST"),
        Spec("EMPTY", sas_format="BEST"),
    ]
    data = {
        "TINY": np.array([0.0, 1.0, -32_768.0, 32_767.0] * 5),
        "MEDIUM": np.array([0.0, 40_000.0, -2_000_000_000.0, 1.0] * 5),
        "LARGE": np.array([0.0, 5_000_000_000.0, 1.0, -1.0] * 5),
        "MONEY": np.array([0.01, 1_234.56, -99.99, 0.0] * 5),
        "RATE": np.array([0.1, 1 / 3, 2 / 7, 1.0] * 5),
        "EMPTY": np.full(20, np.nan),
    }
    # Deliberately several chunks: integrality and scale are conjunctions over
    # the whole file, and a per-chunk answer would be a different answer.
    datasets, options = run(tmp_path, zipped(tmp_path, "t", specs, data), chunk_rows=7)
    kinds = types_of(datasets[0], options)

    assert kinds["TINY"] == "smallint"
    assert kinds["MEDIUM"] == "integer"
    assert kinds["LARGE"] == "bigint"
    assert kinds["MONEY"] == "numeric(8,2)"
    assert kinds["RATE"] == "double precision"
    assert kinds["EMPTY"] == "double precision"


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        # One decimal place is still exactly representable, so the column is
        # demoted to numeric rather than all the way to a binary float.
        (29.5, "numeric(5,1)"),
        # Nothing up to --max-scale represents a third, so it is a float.
        (1 / 3, "double precision"),
    ],
)
def test_a_column_that_turns_fractional_late_is_not_an_integer(
    tmp_path, last, expected
) -> None:
    """The trap in one-pass inference: chunk one looks like a key column.

    Integrality is a property of the whole file, so a fraction in the last chunk
    has to undo an integer type the first chunk would have justified -- and the
    values already written as ``0``, ``1``, ``2`` still have to load into
    whatever it is demoted to. They do: every Postgres numeric type reads ``0``.
    """
    values = np.arange(30.0)
    values[29] = last
    specs = [Spec("LOOKS_WHOLE", sas_format="BEST"), Spec("OTHER", sas_format="BEST")]
    data = {"LOOKS_WHOLE": values, "OTHER": np.ones(30)}
    datasets, options = run(
        tmp_path, zipped(tmp_path, "late", specs, data), chunk_rows=10
    )
    assert types_of(datasets[0], options)["LOOKS_WHOLE"] == expected
    lines = datasets[0].csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "0,1", "the integral chunks are still written without a point"


def test_whole_numbers_are_never_written_with_a_decimal_point(tmp_path) -> None:
    """``1.0`` does not load into ``bigint``, and that is the whole reason."""
    specs = [Spec("ID", sas_format="BEST"), Spec("RATE", sas_format="BEST")]
    data = {"ID": np.array([1.0, 2.0, np.nan, 4.0]), "RATE": np.array([1.0, 2.5, 3.0, np.nan])}
    datasets, options = run(tmp_path, zipped(tmp_path, "w", specs, data), chunk_rows=2)

    text = datasets[0].csv_path.read_text(encoding="utf-8").splitlines()
    assert text[0] == "id,rate"
    assert text[1] == "1,1.0"
    assert text[3] == ",3.0"
    assert types_of(datasets[0], options)["ID"] == "smallint"


def test_whole_numbers_beyond_float_precision_do_not_become_bigint(tmp_path) -> None:
    """Past 2^53 the source is already approximate; bigint would deny it."""
    specs = [Spec("REF", sas_format="BEST")]
    data = {"REF": np.array([9_007_199_254_740_995.0] * 4)}
    datasets, options = run(tmp_path, zipped(tmp_path, "big", specs, data))
    assert types_of(datasets[0], options)["REF"] == "numeric(38,0)"
    assert any("2^53" in note for note in column(datasets[0], "REF").notes)


def test_text_columns_take_the_declared_width(tmp_path) -> None:
    specs = [Spec("NAME", numeric=False, width=40, sas_format="$")]
    data = {"NAME": np.array(["short"] * 4, dtype=object)}
    archive = zipped(tmp_path, "txt", specs, data)

    datasets, options = run(tmp_path / "v", archive)
    assert types_of(datasets[0], options)["NAME"] == "varchar(40)"

    datasets, options = run(tmp_path / "t", archive, text_type="text")
    assert types_of(datasets[0], options)["NAME"] == "text"


def test_int_policy_bigint_gives_every_key_room(tmp_path) -> None:
    specs = [Spec("ID", sas_format="BEST")]
    data = {"ID": np.array([1.0, 2.0, 3.0])}
    datasets, options = run(
        tmp_path, zipped(tmp_path, "p", specs, data), int_policy="bigint"
    )
    assert types_of(datasets[0], options)["ID"] == "bigint"


# --------------------------------------------------------------------------- #
# Dates, datetimes and times
# --------------------------------------------------------------------------- #


def test_dates_and_datetimes_become_iso_text(tmp_path) -> None:
    specs = [
        Spec("D", sas_format="DATE", format_width=9),
        Spec("DT", sas_format="DATETIME", format_width=20),
        Spec("T", sas_format="TIME", format_width=8),
    ]
    data = {
        # epoch, a known date, the 9999 sentinel, before 1960, missing
        "D": np.array([0.0, 23_011.0, SENTINEL_DATE, -1.0, np.nan]),
        "DT": np.array([0.0, 1_700_000_000.0, 0.0, -1.0, np.nan]),
        "T": np.array([0.0, 3_661.0, 86_399.0, 43_200.0, np.nan]),
    }
    datasets, options = run(tmp_path, zipped(tmp_path, "d", specs, data))
    kinds = types_of(datasets[0], options)
    assert (kinds["D"], kinds["DT"], kinds["T"]) == ("date", "timestamp", "time")

    rows = rows_of(datasets[0].csv_path)
    assert [row["d"] for row in rows] == [
        "1960-01-01", "2023-01-01", "9999-12-31", "1959-12-31", "",
    ]
    assert [row["dt"] for row in rows] == [
        "1960-01-01T00:00:00", "2013-11-13T22:13:20",
        "1960-01-01T00:00:00", "1959-12-31T23:59:59", "",
    ]
    assert [row["t"] for row in rows] == [
        "00:00:00", "01:01:01", "23:59:59", "12:00:00", "",
    ]


def test_fractional_seconds_survive(tmp_path) -> None:
    specs = [Spec("DT", sas_format="DATETIME", format_width=26)]
    data = {"DT": np.array([1.5, 2.25, 0.0])}
    datasets, _ = run(tmp_path, zipped(tmp_path, "f", specs, data))
    assert [row["dt"] for row in rows_of(datasets[0].csv_path)] == [
        "1960-01-01T00:00:01.500000",
        "1960-01-01T00:00:02.250000",
        "1960-01-01T00:00:00.000000",
    ]


def test_temporals_outside_what_postgres_accepts_become_null(tmp_path) -> None:
    """Better one reported null than a COPY that dies at row 400,000."""
    specs = [
        Spec("D", sas_format="DATE", format_width=9),
        Spec("T", sas_format="TIME", format_width=8),
    ]
    data = {
        "D": np.array([0.0, 4_000_000.0, -800_000.0]),
        "T": np.array([0.0, 90_000.0, -5.0]),
    }
    datasets, _ = run(tmp_path, zipped(tmp_path, "oor", specs, data))
    rows = rows_of(datasets[0].csv_path)
    assert [row["d"] for row in rows] == ["1960-01-01", "", ""]
    assert [row["t"] for row in rows] == ["00:00:00", "", ""]
    assert column(datasets[0], "D").n_out_of_range == 2
    assert any("outside the range" in note for note in column(datasets[0], "D").notes)


def test_an_ambiguous_date_format_stays_numeric_and_says_so(tmp_path) -> None:
    """YEAR4. on the integer 2024 is a date in SAS and nonsense as 1965-07-17."""
    specs = [Spec("STATEMENT_YEAR", sas_format="YEAR", format_width=4)]
    data = {"STATEMENT_YEAR": np.array([2023.0, 2024.0, 2025.0])}
    archive = zipped(tmp_path, "amb", specs, data)

    strict, options = run(tmp_path / "s", archive)
    assert types_of(strict[0], options)["STATEMENT_YEAR"] == "smallint"
    assert [r["statement_year"] for r in rows_of(strict[0].csv_path)] == ["2023", "2024", "2025"]
    assert any("--treat-as-date" in note for note in column(strict[0], "STATEMENT_YEAR").notes)

    broad, options = run(tmp_path / "b", archive, date_formats="broad")
    assert types_of(broad[0], options)["STATEMENT_YEAR"] == "date"
    assert [r["statement_year"] for r in rows_of(broad[0].csv_path)][0] == "1965-07-16"

    forced, options = run(
        tmp_path / "f", archive, treat_as_date=frozenset({"STATEMENT_YEAR"})
    )
    assert types_of(forced[0], options)["STATEMENT_YEAR"] == "date"


def test_a_date_column_can_be_forced_back_to_numeric(tmp_path) -> None:
    """What comes out is the SAS day number, which is what was asked for."""
    specs = [Spec("D", sas_format="DATE", format_width=9)]
    data = {"D": np.array([0.0, 23_011.0])}
    datasets, options = run(
        tmp_path, zipped(tmp_path, "n", specs, data),
        treat_as_numeric=frozenset({"D"}),
    )
    assert types_of(datasets[0], options)["D"] == "smallint"
    assert [r["d"] for r in rows_of(datasets[0].csv_path)] == ["0", "23011"]


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def test_text_is_trimmed_nul_free_and_blank_means_null(tmp_path) -> None:
    specs = [
        Spec("S", numeric=False, width=20, sas_format="$"),
        Spec("N", sas_format="BEST"),
    ]
    data = {
        "S": np.array(
            ["padded", "  keep leading", "", "    ", "with\x00nul", "ok"], dtype=object
        ),
        "N": np.arange(6.0),
    }
    datasets, _ = run(tmp_path, zipped(tmp_path, "s", specs, data))

    # A CSV reader cannot tell "" from NULL here, and neither can Postgres --
    # that is the documented behaviour -- so the raw lines are what is asserted.
    lines = datasets[0].csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[1:] == [
        "padded,0", "  keep leading,1", ",2", ",3", "withnul,4", "ok,5",
    ]
    assert column(datasets[0], "S").n_nul_stripped == 1
    assert any("NUL" in note for note in column(datasets[0], "S").notes)


def test_awkward_text_survives_a_csv_round_trip(tmp_path) -> None:
    awkward = ['quote"inside', "comma,inside", "line\nbreak", "tab\there", "plain"]
    specs = [Spec("S", numeric=False, width=40, sas_format="$")]
    datasets, _ = run(
        tmp_path,
        zipped(tmp_path, "q", specs, {"S": np.array(awkward, dtype=object)}),
    )
    assert [row["s"] for row in rows_of(datasets[0].csv_path)] == awkward


def unnamed_encoding_archive(tmp_path: pathlib.Path) -> pathlib.Path:
    """A file declaring an encoding code no table has a name for."""
    specs = [
        Spec("S", numeric=False, width=12, sas_format="$"),
        Spec("N", sas_format="BEST"),
    ]
    data = {"S": np.array(["caf\xe9", "x"], dtype=object), "N": np.array([1.0, 2.0])}
    path = write_sas7bdat(tmp_path / "e.sas7bdat", specs, data, encoding="latin1")
    raw = bytearray(path.read_bytes())
    raw[70] = 199  # not in anybody's table
    path.write_bytes(bytes(raw))

    archive = tmp_path / "e.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.write(path, path.name)
    return archive


def test_an_unrecognised_encoding_falls_back_rather_than_failing(tmp_path) -> None:
    """Three of nine real-world files declare an encoding pandas cannot name."""
    archive = unnamed_encoding_archive(tmp_path)
    datasets, _ = run(tmp_path, archive, fallback_encoding="latin1")
    assert [row["s"] for row in rows_of(datasets[0].csv_path)] == ["café", "x"]


def test_falling_back_on_the_encoding_does_not_rewind_the_zip(tmp_path) -> None:
    """The reason the encoding is sniffed rather than inferred by the reader.

    Letting the reader infer it means recovering from its LookupError by
    constructing a second reader, and by then the header has been read -- so the
    stream has to be rewound, which on a zip means decompressing the member from
    the start again. Reading one byte of the header first avoids that, and this
    is the case where the difference shows.
    """
    archive = unnamed_encoding_archive(tmp_path)
    assert rewinds_during(tmp_path, archive, fallback_encoding="latin1") == 0


# --------------------------------------------------------------------------- #
# Identifiers, DDL and the load script
# --------------------------------------------------------------------------- #


def test_sas_names_become_unique_lower_case_identifiers() -> None:
    taken: set[str] = set()
    assert sql_identifier("POL_ID", taken) == "pol_id"
    assert sql_identifier("Total £ Amount", taken) == "total_amount"
    assert sql_identifier("2024_TOTAL", taken) == "c_2024_total"
    assert sql_identifier("POL_ID", taken) == "pol_id_2"
    assert sql_identifier("POL_ID", taken) == "pol_id_3"
    assert sql_identifier("select", taken) == "select"  # quoted at the point of use

    long = sql_identifier("X" * 90, taken)
    assert len(long.encode("utf-8")) <= 63
    again = sql_identifier("X" * 90, taken)
    assert again != long and len(again.encode("utf-8")) <= 63


def test_normalise_format_agrees_across_readers() -> None:
    assert normalise_format("DATETIME28.9") == "DATETIME"
    assert normalise_format("DATETIME") == "DATETIME"
    assert normalise_format("MMDDYY10.") == "MMDDYY"
    assert normalise_format("$CHAR20") == "CHAR"
    assert normalise_format("E8601DA") == "E8601DA"
    assert normalise_format(b"date9") == "DATE"
    assert normalise_format(None) == ""


def test_the_ddl_records_where_every_column_came_from(tmp_path) -> None:
    specs = [
        Spec("POL_ID", sas_format="BEST", label="Policy number"),
        Spec("SURNAME", numeric=False, width=30, sas_format="$", label="Family name"),
    ]
    data = {"POL_ID": np.array([1.0]), "SURNAME": np.array(["x"], dtype=object)}
    datasets, _ = run(tmp_path, zipped(tmp_path, "ddl", specs, data))

    ddl = datasets[0].ddl_path.read_text(encoding="utf-8")
    assert 'CREATE TABLE IF NOT EXISTS "public"."ddl"' in ddl
    assert '"pol_id"' in ddl and "POL_ID num BEST -- Policy number" in ddl
    assert "SURNAME char len 30 -- Family name" in ddl


def test_the_load_script_copies_with_the_options_the_csv_was_written_for(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST"), Spec("B", numeric=False, width=4)]
    data = {"A": np.array([1.0]), "B": np.array(["x"], dtype=object)}
    datasets, options = run(tmp_path, zipped(tmp_path, "load", specs, data))
    script = render_load_script(datasets, options)

    assert "FORMAT csv" in script
    assert "HEADER true" in script
    assert "NULL ''" in script
    assert "ENCODING 'UTF8'" in script
    assert '\\copy "public"."load" ("a", "b")' in script
    assert "BEGIN;" in script and "COMMIT;" in script
    # Absolute, forward-slashed: a Windows backslash inside a psql literal is
    # not something to find out about at load time.
    assert datasets[0].csv_path.resolve().as_posix() in script
    copy_line = next(
        line for line in script.splitlines() if line.startswith("\\copy")
    )
    assert "\\" not in copy_line.split("FROM", 1)[1].split("WITH", 1)[0]


def test_a_single_column_csv_is_quoted_so_a_lone_backslash_dot_cannot_end_it(tmp_path) -> None:
    r"""``\.`` alone on a line ends a COPY. One column is where that can happen."""
    specs = [Spec("ONLY", numeric=False, width=8, sas_format="$")]
    data = {"ONLY": np.array(["a", "\\.", "b"], dtype=object)}
    datasets, options = run(tmp_path, zipped(tmp_path, "one", specs, data))

    lines = datasets[0].csv_path.read_text(encoding="utf-8").splitlines()
    assert lines == ['"only"', '"a"', '"\\."', '"b"']
    assert "FORCE_NULL" in render_load_script(datasets, options)


# --------------------------------------------------------------------------- #
# Operational behaviour
# --------------------------------------------------------------------------- #


def test_an_existing_csv_is_not_silently_replaced(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST")]
    archive = zipped(tmp_path, "keep", specs, {"A": np.array([1.0])})

    first, _ = run(tmp_path, archive)
    assert first and first[0].rows == 1

    again, _ = run(tmp_path, archive)
    assert again == []

    third, _ = run(tmp_path, archive, overwrite=True)
    assert third and third[0].rows == 1


def test_a_failed_conversion_leaves_no_half_written_csv(tmp_path, monkeypatch) -> None:
    """A partial CSV is a file somebody eventually loads."""
    import scripts.sas2csv as module

    specs = [Spec("A", sas_format="BEST"), Spec("B", numeric=False, width=8)]
    data = {"A": np.arange(500.0), "B": np.array(["x"] * 500, dtype=object)}
    archive = zipped(tmp_path, "boom", specs, data)

    calls = {"n": 0}
    original = module._text_to_list

    def explode(values, col, encoding, errors):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("disk went away")
        return original(values, col, encoding, errors)

    monkeypatch.setattr(module, "_text_to_list", explode)
    with pytest.raises(RuntimeError):
        run(tmp_path, archive, chunk_rows=100)
    assert not (tmp_path / "out" / "boom.csv").exists()


def test_every_sas7bdat_in_the_archive_is_converted(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST")]
    one = write_sas7bdat(tmp_path / "one.sas7bdat", specs, {"A": np.array([1.0])})
    two = write_sas7bdat(tmp_path / "two.sas7bdat", specs, {"A": np.array([2.0, 3.0])})
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.write(one, "extracts/one.sas7bdat")
        handle.write(two, "extracts/two.sas7bdat")
        handle.writestr("readme.txt", "not a dataset")

    datasets, _ = run(tmp_path, archive)
    assert sorted(d.table for d in datasets) == ["one", "two"]
    assert sorted(d.rows for d in datasets) == [1, 2]


def test_two_archives_holding_the_same_name_do_not_overwrite_each_other(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST")]
    first = zipped(tmp_path / "a", "same", specs, {"A": np.array([1.0])})
    second = zipped(tmp_path / "b", "same", specs, {"A": np.array([2.0])})

    options = Options(out=tmp_path / "out", quiet=True)
    datasets = convert([first, second], options)
    assert len(datasets) == 2
    assert len({d.csv_path for d in datasets}) == 2
    assert len({d.table for d in datasets}) == 2


def test_the_table_name_can_be_prefixed_and_schema_qualified(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST")]
    archive = zipped(tmp_path, "policy", specs, {"A": np.array([1.0])})
    datasets, _ = run(tmp_path, archive, schema="staging", table_prefix="sas_")
    assert datasets[0].table == "sas_policy"
    assert datasets[0].qualified == '"staging"."sas_policy"'


def test_the_row_count_in_the_header_is_checked_against_what_was_read(tmp_path) -> None:
    specs = [Spec("A", sas_format="BEST")]
    archive = zipped(
        tmp_path, "short", specs, {"A": np.arange(10.0)}, declared_rows=10
    )
    datasets, _ = run(tmp_path, archive)
    assert datasets[0].rows == datasets[0].declared_rows == 10
    assert datasets[0].warnings == []


# --------------------------------------------------------------------------- #
# The only test that proves the output is loadable
# --------------------------------------------------------------------------- #


def _psql_dsn() -> str | None:
    dsn = os.environ.get("CMDM_TEST_DSN") or os.environ.get("CMDM_DSN")
    if not dsn or not shutil.which("psql"):
        return None
    return dsn


@pytest.mark.skipif(_psql_dsn() is None, reason="no psql and CMDM_TEST_DSN/CMDM_DSN")
def test_the_generated_ddl_and_copy_actually_load(tmp_path) -> None:
    """Everything else here proves self-consistency. This proves it loads."""
    dsn = _psql_dsn()
    assert dsn is not None
    specs = [
        Spec("POL_ID", sas_format="BEST"),
        Spec("PREMIUM", sas_format="DOLLAR", format_width=12),
        Spec("INCEPT_DT", sas_format="DATE", format_width=9),
        Spec("EXPIRY_DT", sas_format="DATE", format_width=9),
        Spec("LOADED_AT", sas_format="DATETIME", format_width=20),
        Spec("CALL_TIME", sas_format="TIME", format_width=8),
        Spec("SURNAME", numeric=False, width=30, sas_format="$"),
        Spec("NOTES", numeric=False, width=60, sas_format="$"),
    ]
    data = {
        "POL_ID": np.arange(1.0, 101.0),
        "PREMIUM": np.round(np.linspace(0.01, 9_999.99, 100), 2),
        "INCEPT_DT": np.linspace(14_000, 24_000, 100).round(),
        "EXPIRY_DT": np.array([SENTINEL_DATE] * 50 + [23_011.0] * 50),
        "LOADED_AT": np.linspace(0, 2e9, 100).round(),
        "CALL_TIME": np.linspace(0, 86_399, 100).round(),
        "SURNAME": np.array(
            ['quote"inside', "comma,inside", "line\nbreak", "Ünicode", ""] * 20,
            dtype=object,
        ),
        "NOTES": np.array(["with\x00nul", "   ", "plain", "", "x"] * 20, dtype=object),
    }
    archive = zipped(tmp_path, "loadable", specs, data)
    datasets, options = run(tmp_path, archive, schema="public")
    load = options.out / "load.sql"
    load.write_text(render_load_script(datasets, options), encoding="utf-8")

    def query(sql: str) -> str:
        result = subprocess.run(
            ["psql", dsn, "-tAc", sql], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    query("drop table if exists public.loadable")
    done = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(load)],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr

    assert query("select count(*) from public.loadable") == "100"
    assert query("select premium from public.loadable where pol_id = 1") == "0.01"
    assert query("select expiry_dt from public.loadable where pol_id = 1") == "9999-12-31"
    assert query("select call_time from public.loadable where pol_id = 100") == "23:59:59"
    assert query(
        "select count(*) from public.loadable where surname is null"
    ) == "20"
    assert query(
        r"select surname from public.loadable where pol_id = 3"
    ).replace("\n", "|") == "line|break"
    assert query(
        "select notes from public.loadable where pol_id = 1"
    ) == "withnul"
    # The types the DDL asked for are the types that exist.
    assert query(
        "select data_type from information_schema.columns "
        "where table_name = 'loadable' and column_name = 'premium'"
    ) == "numeric"
    assert query(
        "select data_type from information_schema.columns "
        "where table_name = 'loadable' and column_name = 'pol_id'"
    ) == "smallint"


@pytest.mark.skipif(_psql_dsn() is None, reason="no psql and CMDM_TEST_DSN/CMDM_DSN")
def test_running_the_load_script_twice_refuses_instead_of_duplicating(tmp_path) -> None:
    """Re-running a load is a normal thing to do after fixing something.

    ``CREATE TABLE IF NOT EXISTS`` followed by ``COPY`` would quietly append a
    second copy of half a million rows, which is a bad afternoon. The generated
    script checks each table for rows and stops.
    """
    dsn = _psql_dsn()
    assert dsn is not None
    specs = [Spec("A", sas_format="BEST"), Spec("B", numeric=False, width=4)]
    data = {"A": np.arange(1.0, 6.0), "B": np.array(["x"] * 5, dtype=object)}
    datasets, options = run(tmp_path, zipped(tmp_path, "twice", specs, data))
    load = options.out / "load.sql"
    load.write_text(render_load_script(datasets, options), encoding="utf-8")

    subprocess.run(
        ["psql", dsn, "-tAc", "drop table if exists public.twice"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    first = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(load)],
        capture_output=True, text=True, timeout=60,
    )
    assert first.returncode == 0, first.stderr

    second = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(load)],
        capture_output=True, text=True, timeout=60,
    )
    assert second.returncode != 0
    assert "already holds rows" in second.stderr

    count = subprocess.run(
        ["psql", dsn, "-tAc", "select count(*) from public.twice"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert count.stdout.strip() == "5", "the refused load must not have added rows"
