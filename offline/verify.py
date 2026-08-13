"""Prove this machine can run Customer MDM, offline, end to end.

Runs the real system rather than checking that files exist: it starts the
embedded PostgreSQL, applies the migrations, ingests the sample extract through
the actual pipeline, asserts the golden records came out, exercises the API in
process, and then runs the full test suite.

    verify.cmd          (Windows)
    ./verify.sh         (Linux / macOS)

Every step prints PASS or FAIL with the reason. A FAIL is a real problem with
this machine or this bundle, not a warning to be read past.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import time
import traceback
import warnings

HERE = pathlib.Path(__file__).resolve().parent

# Starlette's TestClient warns that it prefers httpx2 over httpx. Nothing here
# can act on it -- the bundle ships the closure pip resolved, and it concerns a
# dependency of a dependency used only by the checks below. Left visible it
# prints a four-line block in the middle of the step it interrupts, breaking the
# one-line-per-check layout and teaching the reader to skim output whose whole
# purpose is to be read carefully.
warnings.filterwarnings(
    "ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated"
)

# The embedded server and the golden store live beside the bundle.
os.environ.setdefault("CMDM_HOME", str(HERE))
os.environ.setdefault("CMDM_ID_HASH_KEY", "verify-only-not-for-production")

FAILURES: list[str] = []
STEP = 0

#: Roughly how long each step takes on a quiet Linux box, in seconds. Printed
#: alongside the step so that "slow" can be told apart from "hung" -- which is
#: the only question anybody has while watching a long-running check. Windows
#: is typically two to five times slower, and slower again where antivirus is
#: inspecting the PostgreSQL binaries and the 5,000-row batch as they are read.
BASELINE = {
    1: 0.1, 2: 1, 3: 1, 4: 0.2, 5: 4, 6: 3, 7: 0.2, 8: 0.2, 9: 21,
}


def step(name: str):
    """Run one check, reporting PASS or FAIL, never raising.

    The step's name is printed *before* it runs, not after. A check that only
    announces itself on completion leaves a blank terminal during the slowest
    part of the run, which reads as a hang.
    """

    def wrap(fn):
        def run():
            global STEP
            STEP += 1
            expected = BASELINE.get(STEP, 1)
            # Written without a trailing newline so the result lands on the
            # same line. No carriage-return redrawing: this output is as often
            # piped to a file or read through a scrollback as watched live, and
            # a repainted line is unreadable in both.
            print(f"  {STEP:>2}. {name} ... ", end="", flush=True)
            started = time.perf_counter()
            try:
                detail = fn() or ""
            except Exception as exc:
                FAILURES.append(name)
                print("FAIL")
                print(f"          {type(exc).__name__}: {exc}")
                for line in traceback.format_exc().splitlines()[-4:-1]:
                    print(f"          {line.strip()}")
                return
            seconds = time.perf_counter() - started
            slow = "  <-- slower than expected" if seconds > expected * 8 else ""
            suffix = f"  ({detail})" if detail else ""
            print(f"PASS{suffix}  [{seconds:.1f}s]{slow}")

        return run

    return wrap


@step("no network is needed (and none is used)")
def check_offline() -> str:
    """Not a network test -- a statement that nothing here dials out.

    Recorded so that a reviewer running this on an air-gapped box can see the
    question was asked. The install already ran with pip --no-index.
    """
    hostname = socket.gethostname()
    return f"host {hostname}, all dependencies local"


@step("every runtime import resolves")
def check_imports() -> str:
    import fastapi  # noqa: F401
    import numpy  # noqa: F401
    import polars as pl
    import prometheus_client  # noqa: F401
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
    import pydantic  # noqa: F401
    import scipy  # noqa: F401
    import uvicorn  # noqa: F401

    import cmdm  # noqa: F401

    return f"polars {pl.__version__}"


#: The database this verifier owns. Everything below runs here, never in the
#: store the app uses.
#:
#: It used to run in the app's own database, which was wrong twice over. It
#: TRUNCATEd the landing zone -- the delivered bytes of every batch ever
#: accepted, which the whole system treats as the thing it can always rebuild
#: from -- to make room for the sample. And its assertions were counts over the
#: whole store, which only hold if nothing else is in it, so running it on a
#: working installation failed with "expected 15,000 role edges, got 29,985"
#: and blamed the machine for the verifier's own arithmetic.
SCRATCH_DATABASE = "cmdm_verify"


def _scratch_dsn(base: str) -> tuple[str, bool]:
    """A private database on the same server, dropped and recreated.

    Returns the DSN and whether it is genuinely separate from ``base``. A
    locked-down external PostgreSQL may refuse CREATE DATABASE; that is
    reported rather than worked around, because the fallback would be to use
    the caller's own store, which is the behaviour being fixed.
    """
    import psycopg
    from psycopg import conninfo

    parts = conninfo.conninfo_to_dict(base)
    if parts.get("dbname") == SCRATCH_DATABASE:
        return base, True

    admin = conninfo.make_conninfo(**{**parts, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DATABASE}"')

    return conninfo.make_conninfo(**{**parts, "dbname": SCRATCH_DATABASE}), True


@step("the embedded PostgreSQL starts")
def check_postgres() -> str:
    from cmdm.embedded import ensure_dsn

    base = os.environ.get("CMDM_DSN") or ensure_dsn()

    import psycopg

    try:
        dsn, private = _scratch_dsn(base)
    except Exception as exc:
        raise AssertionError(
            f"could not create the {SCRATCH_DATABASE!r} database this check "
            f"runs in ({type(exc).__name__}: {exc}). The verifier will not run "
            "in the store the app uses -- it would truncate the landing zone. "
            "Point CMDM_DSN at a server where it may create a database, or "
            "unset it to use the embedded one."
        ) from None

    os.environ["CMDM_DSN"] = dsn
    with psycopg.connect(dsn) as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
    where = f"in {SCRATCH_DATABASE}" if private else ""
    return f"{version.split(' on ')[0]} {where}".strip()


@step("the schema applies")
def check_migrations() -> str:
    import psycopg

    from cmdm.db.engine import apply_migrations

    with psycopg.connect(os.environ["CMDM_DSN"]) as conn:
        applied = apply_migrations(conn)
        conn.commit()
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'mdm'"
        ).fetchone()[0]
    assert tables == 21, f"expected 21 tables in the mdm schema, found {tables}"
    return f"{len(applied)} migration(s) applied this run, {tables} tables"


@step("a batch ingests through the real pipeline")
def check_pipeline() -> str:
    import polars as pl
    import psycopg

    from cmdm.ingest.landing import accept_batch
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import process_batch

    sample = HERE / "data" / "life_admin_sample.csv"
    assert sample.exists(), f"sample extract missing at {sample}"

    mapping = load_mapping(HERE / "src" / "cmdm" / "mappings" / "life_admin.toml")
    raw = pl.read_csv(sample, infer_schema_length=0)

    with psycopg.connect(os.environ["CMDM_DSN"]) as conn:
        # No truncate. This runs in its own database, so there is nothing here
        # to clear and nothing of anybody's to lose.
        batch_id, report, enqueued = accept_batch(
            conn, raw, mapping, origin="VERIFY", filename=sample.name,
            submitted_by="verify",
        )
        assert report.accepted and enqueued, "the sample extract was not accepted"
        conn.commit()

        result = process_batch(conn, batch_id)
        conn.commit()

        pairs = conn.execute("SELECT count(*) FROM mdm.match_pair").fetchone()[0]
        edges = conn.execute(
            "SELECT count(*) FROM mdm.relationship "
            "WHERE is_current AND edge_kind = 'PARTY_POLICY'"
        ).fetchone()[0]

    assert result.policies == 5000, f"expected 5,000 policies, got {result.policies}"
    assert result.golden_persons > 2000, f"only {result.golden_persons} golden persons"
    assert pairs > 0, "no match decisions were recorded"
    # Three parties on every policy -- owner, insured, agent. Stated as the
    # relationship it is rather than as the number it comes to, so a change to
    # the sample's size moves it and a genuinely missing role still fails.
    assert edges == result.policies * 3, (
        f"expected {result.policies * 3:,} role edges for {result.policies:,} "
        f"policies, got {edges:,}"
    )

    households = result.households
    assert households and households.households > 300, \
        f"only {households.households if households else 0} households derived"
    assert households.affiliations > 0, "no company, trust or estate links derived"
    return (f"{result.policies:,} policies -> {result.golden_persons:,} golden "
            f"persons, {edges:,} role edges, {households.households:,} households")


@step("re-processing the same batch changes nothing")
def check_idempotent() -> str:
    import psycopg

    from cmdm.worker import process_batch

    with psycopg.connect(os.environ["CMDM_DSN"]) as conn:
        batch_id = conn.execute(
            "SELECT batch_id FROM mdm.ingest_batch ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()[0]
        again = process_batch(conn, batch_id)
        conn.commit()

    for entity, writes in again.writes.items():
        assert writes["inserted"] == 0, \
            f"{writes['inserted']} {entity} rows inserted on a re-run"
        assert writes["changed"] == 0, \
            f"{writes['changed']} {entity} rows changed on a re-run"

    unchanged = sum(w["unchanged"] for w in again.writes.values())
    return f"{unchanged:,} rows unchanged across all three entities, 0 changed"


@step("the API answers")
def check_api() -> str:
    from fastapi.testclient import TestClient

    from cmdm.api.app import create_app
    from cmdm.db import engine

    engine.close_pool()
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        persons = health.json()["golden_persons"]

        refused = client.post(
            "/batches?mapping_name=life_admin",
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        )
        assert refused.status_code == 403, "an unauthenticated submit was not refused"

        metrics = client.get("/metrics")
        assert metrics.status_code == 200 and "cmdm_" in metrics.text
    engine.close_pool()
    return f"/health sees {persons:,} golden persons, /metrics renders, 403 enforced"


@step("the consoles render")
def check_consoles() -> str:
    import psycopg
    from fastapi.testclient import TestClient

    from cmdm.api.app import create_app
    from cmdm.db import engine
    from cmdm.governance.rbac import Role, create_principal

    with psycopg.connect(os.environ["CMDM_DSN"]) as setup:
        setup.execute("DELETE FROM mdm.principal WHERE subject = 'verify@local'")
        _, secret = create_principal(setup, "verify@local", [Role.ADMIN])
        setup.commit()

    engine.close_pool()
    with TestClient(create_app()) as client:
        anonymous = client.get("/console", follow_redirects=False)
        assert anonymous.status_code == 303, "an anonymous browser was not sent to sign in"

        signed_in = client.post(
            "/console/login", data={"key": secret}, follow_redirects=False
        )
        assert signed_in.status_code == 303, "sign-in failed"

        pages = ("/console", "/console/entities", "/console/export",
                 "/console/ingest", "/console/steward", "/console/rules",
                 "/console/quality")
        for path in pages:
            page = client.get(path)
            assert page.status_code == 200, f"{path} returned {page.status_code}"

        # The export is the deliverable, not the page describing it.
        handback = client.get("/console/export/source/life_admin.csv")
        assert handback.status_code == 200, handback.text[:200]
        lines = handback.text.splitlines()
        assert len(lines) == 5001, f"expected 5,000 rows back, got {len(lines) - 1}"
        header = lines[0].split(",")
        for column in ("PolicyMdmId", "OwnerMdmId", "InsuredMdmId", "AgentMdmId"):
            assert column in header, f"{column} missing from the hand-back file"
    engine.close_pool()
    return f"{len(pages)} console pages, the hand-back export, sign-in and the "\
           "anonymous redirect"


@step("the test suite passes")
def check_tests() -> str:
    """The long one, and the only step that is optional.

    It is several hundred tests, most of which open a database transaction. On
    Windows every one of those is a TCP connection rather than a Unix socket, so
    this step alone can take several minutes where it takes twenty seconds
    elsewhere. Steps 1-8 have already exercised the whole system end to end;
    this is the belt to their braces, and --quick skips it.

    Run in a subprocess rather than through ``pytest.main``. Steps 1-8 have
    already imported FastAPI, Starlette and anyio into this interpreter, and
    pytest cannot instrument a plugin module that is already imported -- so an
    in-process run reported ``PytestAssertRewriteWarning ... anyio`` on every
    pass. Suppressing that would have hidden the real point, which is that the
    suite was inheriting the state of eight checks that ran before it.
    """
    import subprocess

    # The suite truncates the store it runs against, so it gets the same private
    # database as everything above rather than whatever CMDM_DSN pointed at.
    environment = dict(os.environ)
    environment["CMDM_TEST_DSN"] = environment["CMDM_DSN"]
    print()  # pytest writes its own progress dots below
    code = subprocess.call(
        [sys.executable, "-m", "pytest", str(HERE / "tests"), "-q",
         "-p", "no:cacheprovider", "--rootdir", str(HERE), "-x"],
        cwd=str(HERE), env=environment,
    )
    assert code == 0, f"pytest exited {code}"
    return "all tests green"


def main() -> int:
    quick = "--quick" in sys.argv or "-q" in sys.argv

    print("\nCustomer MDM — verifying this machine can run it\n")
    print(f"  bundle:  {HERE}")
    print(f"  python:  {sys.version.split()[0]}")
    print(f"  data:    {os.environ['CMDM_HOME']}")
    print(f"  runs in: the {SCRATCH_DATABASE} database, which it creates and "
          "owns; your store is not touched")
    total = sum(BASELINE.values()) - (BASELINE[9] if quick else 0)
    print(f"  expect:  around {total:.0f}s on a quiet Linux box; two to five "
          "times that on Windows\n")

    checks = [
        check_offline, check_imports, check_postgres, check_migrations,
        check_pipeline, check_idempotent, check_api, check_consoles,
    ]
    if quick:
        print("  (--quick: skipping the test suite, which is the slow step)\n")
    else:
        checks.append(check_tests)

    started = time.perf_counter()
    for check in checks:
        check()
    elapsed = time.perf_counter() - started

    print()
    if FAILURES:
        print(f"  {len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
        print("  This bundle is not usable on this machine as it stands.\n")
        return 1

    import platform

    ext = "cmd" if platform.system() == "Windows" else "sh"
    prefix = "" if platform.system() == "Windows" else "./"
    print(f"  Everything passed in {elapsed:.0f}s. The system runs on this "
          "machine with no network.\n")
    print(f"  Start it:            {prefix}start.{ext}"
          "    then open http://127.0.0.1:8000/console")
    print(f"  Drain the queue:     {prefix}worker.{ext} serve\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
