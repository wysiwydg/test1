# Customer MDM — offline install

Everything needed to run this on a Windows machine with **no internet**, **no
database** and **no Python packages installed**. The only prerequisite is
**CPython 3.13**.

---

## What is in the zip

| | |
|---|---|
| `wheels/` | 33 pre-compiled Python packages, all `win_amd64` / cp313 |
| `pgsql/` | **PostgreSQL 16.2 for Windows** — the real server, 41 MB of binaries and DLLs |
| `src/`, `tests/`, `scripts/` | The system and its 433 tests |
| `data/life_admin_sample.csv` | A 5,000-policy synthetic extract, so there is something to load |
| `docs/` | Architecture, data model, and the console operator guide |
| `install.cmd` `verify.cmd` `start.cmd` `worker.cmd` `stop.cmd` `status.cmd` | What you run |

Nothing here reaches the network. The installer runs `pip --no-index`, which
makes that a guarantee rather than a promise: pip is forbidden from consulting
PyPI and fails loudly if a package is missing from `wheels/` instead of quietly
fetching it.

---

## Install

Open a Command Prompt in the extracted folder and run:

```
install.cmd
```

It checks your Python version, creates `.venv\`, installs every wheel into it,
and generates `config.cmd` holding a fresh identifier-hashing secret.

**Keep `config.cmd`.** The key inside it is what every stored API key and every
national-identifier digest was computed with. Regenerating it orphans them, so
the installer never overwrites an existing one.

Then prove it actually works here:

```
verify.cmd
```

This does not check that files exist. It starts the database, applies the
schema, ingests the 5,000-policy sample through the real pipeline, re-runs the
same batch to prove that changes nothing, exercises the API and all five console
pages, and finally runs the whole test suite. Expect roughly this:

```
   1. PASS  no network is needed (and none is used)
   2. PASS  every runtime import resolves           (polars 1.43.2)
   3. PASS  the embedded PostgreSQL starts          (PostgreSQL 16.2)
   4. PASS  the schema applies                      (6 migrations, 21 tables)
   5. PASS  a batch ingests through the real pipeline
                                (5,000 policies -> 2,712 golden persons)
   6. PASS  re-processing the same batch changes nothing
                                (2,712 unchanged, 0 changed)
   7. PASS  the API answers
   8. PASS  the consoles render
   9. PASS  the test suite passes                   (all tests green)
```

Any `FAIL` is a real problem and prints the reason.

**How long it should take.** Each step announces itself before it runs and
prints its own elapsed time, so you can always see what it is doing. On a quiet
Linux box the whole run is about 30 seconds; **on Windows expect two to five
times that**, and longer again if antivirus is inspecting the PostgreSQL
binaries and the sample extract as they are read.

Step 9 — the 441-test suite — is the slow one by a wide margin. Every test that
touches the database opens a connection, and on Windows that is a TCP
connection rather than a Unix socket, so this step alone can run for several
minutes. Steps 1 to 8 have already exercised the whole system end to end, so if
you only want to know whether the machine can run it:

```
verify.cmd --quick
```

That skips the test suite and finishes in well under a minute.

**If it looks stuck**, the step it is on tells you where to look:

| Stuck on | Check |
|---|---|
| 3, the database starting | `pgdata\server.log`, and whether a firewall prompt is waiting behind another window — the server binds `127.0.0.1` |
| 5 or 6, the pipeline | Task Manager: `postgres.exe` should be busy. 5,000 policies is real work |
| 9, the tests | Normal. Use `--quick` if you do not need it |

From another Command Prompt in the bundle folder:

```
status.cmd
```

reports whether the server is initialised, running and on which port, and
prints the tail of `pgdata\server.log`.

Note that plain `python` is *not* the interpreter the system runs under — the
install puts everything in `.venv\`, so a bare `python -m cmdm.embedded` will
say the module does not exist. `status.cmd` and the other `.cmd` scripts use
the right one; to do it by hand it is
`.venv\Scripts\python -m cmdm.embedded status`.

---

## Run

```
start.cmd
```

Starts the embedded PostgreSQL, applies migrations, prints one API key per
console audience, and serves on <http://127.0.0.1:8000>.

**Write the three keys down.** They are shown once — secrets are stored as a
hash and a four-character prefix, so a key not captured here has to be replaced,
not recovered.

| | |
|---|---|
| <http://127.0.0.1:8000/console/login> | paste a key to sign in |
| `/console/ingest` | submit a CSV, watch it land, process the queue |
| `/console` | search customers, golden record, lineage |
| `/console/entities` | what is in Person, Policy and Relationship, and a browser |
| `/console/export` | your extract back with MDM ids, or the golden records |
| `/console/steward` | the grey-zone review queue |
| `/console/quality` | completeness and conformity |

In another Command Prompt, to process submitted batches continuously:

```
worker.cmd serve
```

Without it, batches sit in the queue until you press **Process queued batches
now** on the ingestion console. Both do the same work.

To stop the database when you are finished:

```
stop.cmd
```

`start.cmd` accepts `CMDM_HOST` and `CMDM_PORT` if 8000 is taken:

```
set CMDM_PORT=8080
start.cmd
```

The full walkthrough of each console is in
[`docs/04-operating-the-consoles.md`](docs/04-operating-the-consoles.md).

---

## Updating to a newer release

You do not need this zip again. Nothing in it changes between releases except
this project's own wheel — the PostgreSQL binaries, polars, scipy and the rest
are byte-for-byte the same files. A release ships instead as an **update pack**
of well under a megabyte, built with `python -m scripts.build_update_pack`.

Unzip it anywhere and point it at this folder:

```
update.cmd  C:\path\to\cmdm-offline
```

It reinstalls the wheel from disk with `--no-index` — no network, on either
end — replaces `src\`, `tests\`, `docs\` and `verify.py`, and keeps what it
replaced in `backup-<timestamp>\`. It will not touch `pgdata\`, `pgpassword`
or `config.cmd`: your database, your keys and your identifier-hashing secret
are the irreplaceable part of an installation and no update has business
there. It refuses outright if the release needs a package this bundle does not
already carry, because an update pack cannot add compiled dependencies.

Then:

```
verify.cmd --quick        prove the machine still runs it
worker.cmd backfill       bring the existing golden store forward
```

`backfill` re-runs every batch already in the landing zone through the current
pipeline. That is how a release that computes something the last one did not
fills in the gap without you re-uploading a file — the bytes are already in the
landing zone, which is why the landing zone is immutable and kept. It is
idempotent: rows that are already right stay on the version they are on, and
running it twice does nothing the second time.

---

## Where your data lives

| | |
|---|---|
| `pgdata\` | The PostgreSQL data directory — **this is the golden store** |
| `pgpassword` | The generated superuser password for that instance |
| `config.cmd` | The identifier-hashing key |
| `.venv\` | The installed packages |

Back up `pgdata\`, `pgpassword` and `config.cmd` together; any one alone is
not enough. To start completely over, stop the server and delete `pgdata\`.

Windows has no Unix sockets, so the server listens on `127.0.0.1` on a port
chosen at first start and recorded in `pgdata\cmdm_port`. It is not reachable
from another machine, and a password is generated at `initdb` time rather than
trusting every local account, which `trust` on a TCP socket would.

The database is a real PostgreSQL 16.2 server, just run out of a folder instead
of installed system-wide. Nothing about the golden store is weakened by that —
the schema, the `FOR UPDATE SKIP LOCKED` queue, the partial unique indexes and
the enum types are all exactly what they would be against a system instance.
Only the lifecycle differs.

### Using a PostgreSQL you already have

Set `CMDM_DSN` and the embedded server is never started:

```
set CMDM_DSN=host=dbhost port=5432 user=cmdm dbname=cmdm password=...
start.cmd
```

`CMDM_DSN` always wins. The embedded instance is a fallback for a machine that
has no database, not a layer in front of one that does.

---

## If something goes wrong

**"this bundle contains wheels for Python 3.13 only"** — the compiled packages
cannot be used across Python versions. Run it with CPython 3.13, or ask for a
bundle rebuilt for the version you have. PostgreSQL is unaffected either way:
it ships as plain executables in `pgsql/` and does not care which interpreter
is running.

**`install.cmd` cannot create a virtual environment** — some minimal or
Store-installed Pythons omit `venv`. Install CPython from python.org, or install
into the interpreter directly:

```
python -m pip install --no-index --find-links wheels cmdm[vector,store,api,embedded] pytest httpx pglast
```

The `.cmd` scripts fall back to plain `python` when `.venv\` is absent.

**`start.cmd` says it cannot start the embedded PostgreSQL** — check
`pgdata\` is writable and that no other copy is already running
(`python -m cmdm.embedded status`). Antivirus that quarantines
`pgdata\` or `.venv\Lib\site-packages\pgserver\pginstall\bin\postgres.exe` will
also cause this; those paths may need an exclusion.

**Port 8000 is in use** — set `CMDM_PORT`.

---

## What is deliberately not included

**ONNX Runtime.** Both AI paths — the standardization fallback and the
grey-zone cross-encoder — are optional and default to deterministic reference
implementations written in plain Python. Those are what produced every figure in
the docs. Shipping the runtime without a model file would add 15 MB you could
not use, since the model could not be fetched on an air-gapped machine either.
If you later obtain both, `pip install --no-index --find-links wheels cmdm[ai]`
once the wheel is added to `wheels/`, and point `CMDM_STANDARDIZER_MODEL` or
`CMDM_CROSS_ENCODER_MODEL` at the file. Until then the system tells you plainly
which of the two is missing rather than failing with an import error.

**The interactive `/docs` page.** FastAPI loads Swagger UI's JavaScript from a
public CDN, so that page is blank without internet. The machine-readable
contract at `/openapi.json` is complete and works offline — that is what
tooling consumes. Serving Swagger UI locally means vendoring about 1.5 MB of
third-party JavaScript; say if you want it.

---

## What was verified, and how

Built on Linux, so the honest split is:

**Verified by execution**, on the identical code and the identical scripts with
a Linux wheel set: the installer, the verifier, the embedded PostgreSQL
lifecycle (initdb, start, connect, restart, stop), migrations, the
5,000-policy pipeline, idempotent re-processing, the
API, all console pages, the launcher chain (`start.sh` → live server → sign-in →
`worker.sh`), and all 433 tests.

**Verified by execution under Python 3.13**: the same bundle built for
Linux/cp313, installed and run with `/usr/bin/python3.13` — all nine checks
pass, including the full 433-test suite. The code is 3.13-clean; only the
platform differs.

**Verified by resolution**, for Windows specifically: `pip install --no-index
--find-links wheels --platform win_amd64 --python-version 3.13` resolves and
unpacks the complete dependency closure with no network — 33 packages, 151
Windows `.pyd` extension modules, zero Linux `.so` files. `pgsql/` holds
`initdb.exe`, `pg_ctl.exe`, `pg_isready.exe` and 38 DLLs, all inside that one
tree.

**Not verified**: execution of Windows binaries, which cannot be done from
Linux. That is what `verify.cmd` is for, and why it runs the real system rather
than a smoke test. If it prints nine `PASS` lines, this machine runs it.
