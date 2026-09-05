# SAS7BDAT to Azure Postgres

How `scripts/sas2csv.py` turns a zipped SAS extract into CSV that
`COPY` will accept, what it decides on your behalf, and where it will
tell you it is guessing.

    python -m scripts.sas2csv EXTRACT.zip --out C:\staging

The shape it was written for: half a million rows, two hundred-odd columns,
delivered as a zip, on a Windows machine running Python 3.13 with no internet.
Output is one CSV per dataset, one `CREATE TABLE` beside it, and a `load.sql`
that runs the pair through `psql`.

---

## Measured

| | |
|---|---|
| 500,000 rows x 200 columns | **58.8 s** (8,500 rows/s), read straight out of a 298 MB zip |
| Peak memory | **225 MB**, flat -- it is one streaming pass, not a load |
| Scratch space used | **none**; the 846 MB sas7bdat is never extracted |
| CSV written | 647 MB |
| `COPY` into PostgreSQL 16 | 14 s for all 500,000 rows, 391 MB table |
| Backward seeks in the zip stream | **0** |

Also converted and loaded: nine sas7bdat files produced by real SAS sessions
(Linux and Windows, 32- and 64-bit layouts, two of them RLE-compressed, one 392
columns wide, three declaring an encoding pandas cannot name). All nine load
into PostgreSQL with no errors.

---

## The four decisions

### 1. Nothing is extracted

The sas7bdat is read as a stream out of the zip. This is possible because the
reader only ever moves forward: it seeks to byte zero once, then reads
page-sized blocks to the end. `ZipMemberStream` implements backward seeks anyway
-- by reopening the member and re-reading -- and counts them, because a silently
wrong file is worse than a slow one. The count stays at zero on every file this
has been run against, and a test asserts it.

The alternative costs real money on this data: 500k x 200 doubles is 800 MB
uncompressed, sas7bdat compresses about five-fold inside a zip, so extracting
first means several GB of scratch per file and a copy of the source data left on
disk for somebody to clean up.

`--reader fast` gives up that property deliberately: it spills the member to a
temporary file and reads it with pyreadstat, which is a C library and
substantially quicker. Both readers produce byte-identical CSV, which is also
asserted by a test.

### 2. Types come from the data, not from SAS

SAS has one numeric type: an 8-byte float. Every policy count, every account
number, every premium is the same `double`. Projecting that straight onto
`double precision` is faithful to the storage and useless in a database --
every key you build is then a float key.

So the run accumulates, per column, as it streams: minimum, maximum, whether
every value is a whole number, and whether a fixed number of decimal places
represents every value exactly. The DDL is written afterwards, from that.

| What the data turned out to be | Postgres type |
|---|---|
| Whole numbers within ±32,767 | `smallint` |
| Whole numbers within ±2.1 billion | `integer` |
| Whole numbers within int64 | `bigint` |
| Whole numbers beyond 2^53 | `numeric(38,0)`, and a warning |
| A stable scale up to `--max-scale` (money, almost always) | `numeric(p,s)` |
| Anything else numeric | `double precision` |
| All null | `double precision` |
| Character, width *n* | `varchar(n)` (`--text-type text` for `text`) |
| A full-date format | `date` |
| A datetime format | `timestamp` |
| A time-of-day format | `time` |

Beyond 2^53 a float64 no longer holds consecutive integers, so the source value
is *already* approximate; giving it `bigint` would assert a precision SAS never
had. `--int-policy bigint` gives every whole-number column room to grow instead
of fitting it to the observed range.

`numeric(p,s)` precision is the observed integer digits plus the scale plus two
digits of headroom, capped at 38. Scale inference is not attempted above 2^45,
where the spacing between representable float64 values is wider than the decimal
places being asked about -- there the honest answer is `double precision`.

The one-pass ordering matters and is tested: a column that looks like a key for
40 chunks and turns fractional in the 41st must not be given an integer type,
and the `0`, `1`, `2` already written must still load into whatever it is
demoted to. They do, because every Postgres numeric type reads `0`.

### 3. Dates are converted, and the ambiguous formats are not

A SAS date is days since 1960-01-01; a SAS datetime is seconds since the same
instant; a SAS time is seconds since midnight. Which one a column holds is
carried by its display format, so `DATE9.`, `MMDDYY10.`, `YYMMDD10.`,
`DATETIME20.` and `TIME8.` convert to ISO-8601 text and get `date`, `timestamp`
and `time`.

Some formats display only *part* of a date -- `YEAR4.`, `MONTH.`, `DAY.`,
`QTR.`, `MONNAME.`, `WEEKDAY.`. The stored value is still a date, but those are
also exactly what somebody attaches to a plain integer, and reading the integer
2024 as a SAS date silently yields 1965-07-17. Under the default
`--date-formats strict` those columns stay numeric, and every one is named in
the run's report and in a comment in the generated DDL:

```
! MONTH: format MONNAME is date-valued in SAS but is also how a plain number
  gets displayed, so it was kept numeric; --treat-as-date MONTH converts it
```

That warning is not hypothetical -- it fires on `productsales.sas7bdat`, one of
the real SAS files this was tested against. `--date-formats broad` converts them
all; `--treat-as-date`, `--treat-as-datetime`, `--treat-as-time` and
`--treat-as-numeric` decide one column at a time.

**Why numpy and not pandas timestamps.** `datetime64[ns]` runs out in 2262, and
insurance extracts are full of 31DEC9999 end dates -- 150,333 of them in the
benchmark. Every conversion here is done with `datetime64[s]` or `[us]`, which
reach year 2.9e11, so the sentinel arrives as `9999-12-31` rather than as null.
Values outside what Postgres itself accepts (year 1 to 9999, and 0 to 24 hours
for a time) are written as null and counted, rather than written as something
that would abort a `COPY` at row 400,000.

Datetimes become `timestamp`, not `timestamptz`: a SAS datetime carries no zone,
and inventing one is a decision for whoever knows where the extract came from.

### 4. The CSV is written for `COPY`, not for Excel

UTF-8 with **no BOM**, `\n` line endings, one header row, `"` quoting only
where a value needs it, and empty for null. That is exactly:

```sql
COPY table (columns) FROM '...' WITH (FORMAT csv, HEADER true, NULL '', ENCODING 'UTF8')
```

which is what the generated `load.sql` runs, one transaction per dataset, with
`\i` for the DDL and `ANALYZE` after.

Re-running a load is a normal thing to do after fixing something, and
`CREATE TABLE IF NOT EXISTS` followed by `COPY` would quietly append a second
copy of half a million rows. So each table is checked for rows first and the
script stops rather than duplicate them; loading again deliberately means
`TRUNCATE`-ing the tables, or dropping them and letting the `CREATE` statements
rebuild them.

Four things happen to character data on the way out, each because `COPY` or
Postgres requires it:

* **Trailing blanks go.** SAS pads char fields to their declared width.
* **All-blank becomes null.** That is how SAS spells a missing character value.
  It follows that an empty string and a null are indistinguishable in the
  output, which is accurate for SAS and worth knowing before you write a
  constraint that depends on the difference.
* **NUL bytes are removed and counted.** Postgres cannot store `\x00` in a text
  value at all, and the two ways of loading disagree about what to do with one:
  a server-side `COPY` refuses the file (`invalid byte sequence for encoding
  "UTF8": 0x00`), while psql's `\copy` -- which is what the generated load
  script uses -- silently truncates the value at the NUL and reports success.
  The quiet one is the dangerous one, which is why they are stripped here and
  counted. The benchmark extract had 100,115 of them and the run said so.
* **Undecodable bytes are replaced, not fatal.** Decoding is done a column at a
  time rather than by the reader, so one bad byte in row 400,000 costs one
  character instead of the whole file. `--on-bad-bytes strict` fails instead.

Whole numbers are never written with a decimal point. `1.0` does not load into
`bigint`, and that single detail is why the numeric writer exists.

A dataset with exactly **one** column is written fully quoted, and its `\copy`
carries `FORCE_NULL`. A line consisting of nothing but `\.` ends a `COPY`, and
one column is the only shape where a data value can be alone on a line.

---

## Encodings

The encoding is read from the file header. Three of the nine real files tested
declare one that pandas has no name for, and asking pandas to infer it in that
case fails on the first column name with `LookupError: unknown encoding: infer`
-- so the converter reopens the file under `--fallback-encoding` (default
`cp1252`, which is what Windows SAS sessions write) and says that it did.
`--encoding` overrides the header outright.

Output is always UTF-8, whatever the input was.

---

## Column and table names

SAS names are up to 32 bytes and, with `VALIDVARNAME=ANY`, can contain anything.
Postgres identifiers are 63 bytes and fold to lower case unless quoted.
Everything generated here is quoted at the point of use, so the transformation
exists to make names legible and unique rather than parseable: lower-cased,
runs of non-identifier characters collapsed to one underscore, a leading digit
prefixed with `c_`, truncated to 63 bytes, and a numeric suffix if that
collides with a name already taken. The original SAS name, type, format, width
and label are recorded in a comment against every column in the DDL.

Table names come from the member name inside the zip (`--schema`,
`--table-prefix`). Two archives holding the same member name get distinct
tables rather than one overwriting the other.

---

## Operational behaviour

* **A run that fails leaves no CSV.** A half-written CSV is a file somebody
  eventually loads. On any exception or interrupt the partial file is removed.
* **An existing CSV is never silently replaced.** `--overwrite` does it.
* **Progress is printed.** A 500k-row file is a minute of work; a converter that
  prints nothing for a minute is indistinguishable from one that has hung.
* **`--list`** prints an archive's columns, formats and inferred types without
  converting -- reading only the header, so it is instant on a 5 GB zip.
* **Convert every archive in one run.** `load.sql` covers the datasets of the
  run that wrote it; a second run into the same directory replaces it, and says
  so, naming the tables the old one also loaded.

---

## Offline install

The target has no internet, so the wheels are fetched here and carried there:

    python -m scripts.build_sas2csv_bundle --platform win_amd64 --python 3.13

writes `dist/sas2csv-offline-win_amd64-py313.zip` (about 25 MB): pandas, numpy,
pyreadstat, pytest and their closure, the converter, its tests, and launchers.
On the target: unzip, `install.cmd`, then `convert.cmd EXTRACT.zip --out DIR`.

Wheels are compiled per interpreter version, so the bundle is built for one and
the installer says so loudly if it is run under another.

`pip download --platform win_amd64` chooses wheel *tags* for the target but
evaluates dependency *markers* against the machine doing the downloading, so a
Windows-only requirement is silently skipped when building on Linux -- here
that is `tzdata`, which pandas needs on Windows. The fixpoint loop that catches
it is imported from `build_offline_bundle` rather than copied, and is tested in
`tests/test_offline_bundle.py`.

**Verify it on arrival.** `verify.cmd` runs the test suite on the target. The
tests generate sas7bdat files themselves -- there is no SAS on that machine and
no library writes the format, so `tests/sas7bdat_fixtures.py` writes it: 64-bit,
little-endian, uncompressed. The generated files are read back by *two*
independent implementations (pandas and pyreadstat) and asserted to agree, so a
bug in the generator cannot pass as a converted file.

---

## What is not covered

* **Writing compressed sas7bdat.** The fixtures are uncompressed. Reading
  RLE- and RDC-compressed files is pandas' and pyreadstat's job, and was
  verified against genuinely compressed files from real SAS sessions rather
  than against a re-implementation of the compressor.
* **SAS user-defined formats.** A column with a custom format is converted on
  its stored value; the format catalogue (`.sas7bcat`) is not read, so coded
  values arrive as codes. Value labels would have to come from the catalogue or
  a mapping table.
* **The 28 special missing values.** SAS distinguishes `.A` through `.Z` and
  `._` from `.`; all of them arrive as null. Preserving which one would need a
  second column per numeric column.
* **`timestamptz`.** See above: nothing in the file says what zone it was.
