# Customer MDM — offline install

Everything needed to run this on a Windows machine with **no internet**, **no
database** and **no Python packages installed**. The only prerequisite is
CPython 3.11.

---

## What is in the zip

| | |
|---|---|
| `wheels/` | 36 pre-compiled Python packages, **all `win_amd64` / cp311** — including PostgreSQL 16.2 itself |
| `src/`, `tests/`, `scripts/` | The system and its 433 tests |
| `data/life_admin_sample.csv` | A 5,000-policy synthetic extract, so there is something to load |
| `docs/` | Architecture, data model, and the console operator guide |
| `install.cmd` `verify.cmd` `start.cmd` `worker.cmd` `stop.cmd` | What you run |

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

Any `FAIL` is a real problem and prints the reason. Take about a minute for the
whole run.

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

## Where your data lives

| | |
|---|---|
| `pgdata\` | The PostgreSQL data directory — **this is the golden store** |
| `config.cmd` | The identifier-hashing key |
| `.venv\` | The installed packages |

Back up `pgdata\` and `config.cmd` together; either alone is not enough. To
start completely over, stop the server and delete `pgdata\`.

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

**"this bundle contains wheels for Python 3.11 only"** — the compiled packages
cannot be used across Python versions. Install CPython 3.11, or ask for a
bundle rebuilt for the version you have.

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
lifecycle, migrations, the 5,000-policy pipeline, idempotent re-processing, the
API, all console pages, the launcher chain (`start.sh` → live server → sign-in →
`worker.sh`), and all 433 tests.

**Verified by resolution**, for Windows specifically: `pip install --no-index
--find-links wheels --platform win_amd64 --python-version 3.11` resolves and
unpacks the complete dependency closure with no network — 36 packages, 156
Windows `.pyd` extension modules, zero Linux `.so` files, and PostgreSQL 16.2's
`postgres.exe`. Nothing is missing and nothing is the wrong platform.

**Not verified**: execution of Windows binaries, which cannot be done from
Linux. That is what `verify.cmd` is for, and why it runs the real system rather
than a smoke test. If it prints nine `PASS` lines, this machine runs it.
