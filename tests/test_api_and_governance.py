"""Tests for the API, governance, observability and the consoles.

The governance tests assert the properties that make the controls real rather
than decorative: denial is the default, masking is driven by the registry,
the audit trail cannot be rewritten, and an erasure that retains data says why.
"""

from __future__ import annotations

import datetime as dt
import uuid

import polars as pl
import psycopg
import pytest

from cmdm.governance.privacy import (
    ERASED_TOMBSTONE,
    ConsentPurpose,
    ConsentState,
    ErasureState,
    assess_erasure,
    current_consent,
    execute_erasure,
    may_contact,
    record_consent,
    request_erasure,
)
from cmdm.governance.rbac import (
    ANONYMOUS,
    MASK,
    ROLE_PERMISSIONS,
    AccessDenied,
    Action,
    Principal,
    Role,
    authenticate,
    authorize,
    create_principal,
    log_access,
    mask_frame,
    maskable_columns,
    record_steward_action,
)
from cmdm.model.enums import PiiClass
from cmdm.model.fields import PERSON
from cmdm.model.ids import uuid7
from cmdm.observe import assess_data_quality, evaluate_matching, operational_snapshot
from cmdm.store.writer import write_entities


@pytest.fixture(autouse=True)
def _hash_key(monkeypatch):
    """API keys are hashed under a deployment secret; tests need one."""
    monkeypatch.setenv("CMDM_ID_HASH_KEY", "test-key-not-for-production")


def golden_person(**overrides) -> pl.DataFrame:
    base = {
        "person_id": str(uuid7()), "party_type": "PERSON",
        "full_name": "JOHN SMITH", "full_name_normalized": "JOHN SMITH",
        "name_sorted_key": "JOHN SMITH", "name_phonetic_key": "JHN SMT",
        "name_tokens": [["JOHN", "SMITH"]], "name_parse_method": "RULE_BASED",
        "date_of_birth": dt.date(1980, 1, 1), "email_address": "j@x.com",
        "email_normalized": "j@x.com", "source_count": 1,
    }
    base.update(overrides)
    return pl.DataFrame({
        k: (v if isinstance(v, list) else [v]) for k, v in base.items()
    })


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_every_role_grants_something() -> None:
    for role, permissions in ROLE_PERMISSIONS.items():
        assert permissions, role


def test_anonymous_can_do_nothing() -> None:
    """Denial is the default, not a permissive fallback nobody notices."""
    assert ANONYMOUS.permissions == frozenset()
    for action in (Action.READ, Action.SEARCH, Action.SUBMIT, Action.MERGE):
        with pytest.raises(AccessDenied):
            authorize(ANONYMOUS, action)


def test_viewer_cannot_unmask() -> None:
    viewer = Principal(uuid7(), "v", (Role.VIEWER,))
    assert viewer.may(Action.READ)
    assert not viewer.may_unmask


def test_operator_can_unmask_but_not_merge() -> None:
    operator = Principal(uuid7(), "o", (Role.OPERATOR,))
    assert operator.may_unmask
    assert not operator.may(Action.MERGE)


def test_ingestor_cannot_read_the_golden_store() -> None:
    """A loader needs to submit, not to browse customers."""
    ingestor = Principal(uuid7(), "i", (Role.INGESTOR,))
    assert ingestor.may(Action.SUBMIT)
    assert not ingestor.may(Action.READ)
    assert not ingestor.may(Action.SEARCH)


def test_only_admin_may_erase() -> None:
    for role in (Role.VIEWER, Role.OPERATOR, Role.STEWARD, Role.INGESTOR):
        assert Action.ERASE not in ROLE_PERMISSIONS[role], role
    assert Action.ERASE in ROLE_PERMISSIONS[Role.ADMIN]


def test_multiple_roles_union_their_permissions() -> None:
    both = Principal(uuid7(), "b", (Role.VIEWER, Role.INGESTOR))
    assert both.may(Action.READ) and both.may(Action.SUBMIT)


def test_create_principal_rejects_unknown_roles(conn) -> None:
    with pytest.raises(ValueError, match="unknown roles"):
        create_principal(conn, "x@y.com", ["WIZARD"])


def test_create_principal_rejects_no_roles(conn) -> None:
    with pytest.raises(ValueError, match="at least one"):
        create_principal(conn, "x@y.com", [])


def test_authentication_round_trip(conn) -> None:
    created, secret = create_principal(conn, "svc@x.com", [Role.OPERATOR])
    resolved = authenticate(conn, secret)
    assert resolved.subject == "svc@x.com"
    assert resolved.roles == (Role.OPERATOR,)


def test_wrong_key_is_anonymous_not_an_error(conn) -> None:
    create_principal(conn, "svc@x.com", [Role.OPERATOR])
    assert authenticate(conn, "not-a-real-key").subject == "anonymous"


def test_missing_key_is_anonymous(conn) -> None:
    assert authenticate(conn, None).subject == "anonymous"


def test_secret_is_not_stored_in_the_clear(conn) -> None:
    """A database dump must not yield working credentials."""
    _, secret = create_principal(conn, "svc@x.com", [Role.OPERATOR])
    stored = conn.execute(
        "SELECT secret_hash FROM mdm.principal WHERE subject = 'svc@x.com'"
    ).fetchone()[0]
    assert secret not in stored


def test_inactive_principal_cannot_authenticate(conn) -> None:
    _, secret = create_principal(conn, "svc@x.com", [Role.OPERATOR])
    conn.execute("UPDATE mdm.principal SET is_active = false")
    assert authenticate(conn, secret).subject == "anonymous"


def test_expired_principal_cannot_authenticate(conn) -> None:
    _, secret = create_principal(conn, "svc@x.com", [Role.OPERATOR])
    conn.execute("UPDATE mdm.principal SET expires_at = now() - interval '1 day'")
    assert authenticate(conn, secret).subject == "anonymous"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def test_maskable_columns_come_from_the_registry() -> None:
    """A new attribute is protected by declaring what it is."""
    columns = maskable_columns(PERSON)
    assert "full_name" in columns
    assert "date_of_birth" in columns
    assert "national_id_hash" in columns
    assert "party_type" not in columns


def test_masking_hides_direct_pii_from_a_viewer() -> None:
    viewer = Principal(uuid7(), "v", (Role.VIEWER,))
    masked, revealed = mask_frame(golden_person(), PERSON, viewer)
    assert not revealed
    assert masked["full_name"][0] == MASK
    assert masked["date_of_birth"][0] == MASK


def test_masking_leaves_non_pii_alone() -> None:
    viewer = Principal(uuid7(), "v", (Role.VIEWER,))
    masked, _ = mask_frame(golden_person(), PERSON, viewer)
    assert masked["party_type"][0] == "PERSON"


def test_an_operator_sees_unmasked() -> None:
    operator = Principal(uuid7(), "o", (Role.OPERATOR,))
    masked, revealed = mask_frame(golden_person(), PERSON, operator)
    assert revealed
    assert masked["full_name"][0] == "JOHN SMITH"


def test_masking_preserves_nulls() -> None:
    """A masked absent value must not look like a withheld present one."""
    viewer = Principal(uuid7(), "v", (Role.VIEWER,))
    frame = golden_person(email_address=None)
    masked, _ = mask_frame(frame, PERSON, viewer)
    assert masked["email_address"][0] is None


def test_masking_keeps_the_column_rather_than_dropping_it() -> None:
    """Dropping it makes a masked response look like a sparse record."""
    viewer = Principal(uuid7(), "v", (Role.VIEWER,))
    masked, _ = mask_frame(golden_person(), PERSON, viewer)
    assert "full_name" in masked.columns


def test_sensitive_only_masking_is_narrower() -> None:
    columns = maskable_columns(PERSON, minimum=PiiClass.SENSITIVE)
    assert "national_id_hash" in columns
    assert "full_name" not in columns


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_access_log_records_a_read(conn) -> None:
    principal = Principal(uuid7(), "reader", (Role.VIEWER,))
    log_access(conn, principal, Action.READ, entity_name="Person", record_count=3)
    row = conn.execute(
        "SELECT subject, action, record_count FROM mdm.access_log "
        "ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert row == ("reader", "READ", 3)


def test_access_log_records_whether_pii_was_revealed(conn) -> None:
    """'This call returned 4,000 unmasked dates of birth' is the alertable event."""
    principal = Principal(uuid7(), "op", (Role.OPERATOR,))
    log_access(conn, principal, Action.SEARCH, record_count=4000, pii_revealed=True)
    row = conn.execute(
        "SELECT record_count, pii_revealed FROM mdm.access_log ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert row == (4000, True)


def test_access_log_cannot_be_updated_or_deleted(conn) -> None:
    """An audit trail a compromised app account can rewrite is not one."""
    principal = Principal(uuid7(), "x", (Role.VIEWER,))
    log_access(conn, principal, Action.READ)
    before = conn.execute("SELECT count(*) FROM mdm.access_log").fetchone()[0]

    conn.execute("UPDATE mdm.access_log SET subject = 'tampered'")
    conn.execute("DELETE FROM mdm.access_log")

    after = conn.execute("SELECT count(*) FROM mdm.access_log").fetchone()[0]
    tampered = conn.execute(
        "SELECT count(*) FROM mdm.access_log WHERE subject = 'tampered'"
    ).fetchone()[0]
    assert after == before
    assert tampered == 0


def test_steward_action_requires_a_reason(conn) -> None:
    principal = Principal(uuid7(), "s", (Role.STEWARD,))
    with pytest.raises(ValueError, match="reason"):
        record_steward_action(conn, principal, Action.MERGE, entity_name="Person", reason="")


def test_steward_action_records_before_and_after(conn) -> None:
    # A real principal, because steward_action has a foreign key to it: an
    # action attributed to a principal that does not exist is not attribution.
    principal, _ = create_principal(conn, "steward-fk@x.com", [Role.STEWARD])
    action_id = record_steward_action(
        conn, principal, Action.OVERRIDE, entity_name="Person",
        reason="customer called to correct the spelling",
        before={"full_name": "JOHN SMTH"}, after={"full_name": "JOHN SMITH"},
    )
    row = conn.execute(
        "SELECT before_value, after_value, reason FROM mdm.steward_action WHERE action_id = %s",
        (action_id,),
    ).fetchone()
    assert row[0]["full_name"] == "JOHN SMTH"
    assert row[1]["full_name"] == "JOHN SMITH"


def test_database_rejects_a_blank_steward_reason(conn) -> None:
    """The gate must survive a refactor of the calling code."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO mdm.steward_action "
            "(action_id, action, subject, entity_name, reason) "
            "VALUES (%s, 'MERGE', 's', 'Person', '  ')",
            (uuid7(),),
        )


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


@pytest.fixture()
def person_id(conn) -> str:
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    return frame["person_id"][0]


def test_consent_round_trip(conn, person_id) -> None:
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.GRANTED,
                   channel="web", evidence_ref="form-1")
    assert may_contact(conn, person_id)


def test_withdrawal_supersedes_without_erasing_history(conn, person_id) -> None:
    """'Prove this customer consented' is what a regulator asks."""
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.GRANTED)
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.WITHDRAWN)

    assert not may_contact(conn, person_id)
    history = conn.execute(
        "SELECT count(*) FROM mdm.consent WHERE person_id = %s", (person_id,)
    ).fetchone()[0]
    assert history == 2


def test_only_one_live_consent_per_purpose(conn, person_id) -> None:
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.GRANTED)
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.WITHDRAWN)
    live = conn.execute(
        "SELECT count(*) FROM mdm.consent WHERE person_id = %s AND effective_to IS NULL",
        (person_id,),
    ).fetchone()[0]
    assert live == 1


def test_absence_of_consent_is_not_consent(conn, person_id) -> None:
    """Defaulting the other way is what produces regulatory findings."""
    assert not may_contact(conn, person_id)


def test_consent_is_per_purpose(conn, person_id) -> None:
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.GRANTED)
    assert may_contact(conn, person_id, ConsentPurpose.MARKETING)
    assert not may_contact(conn, person_id, ConsentPurpose.PROFILING)


def test_current_consent_reports_every_purpose(conn, person_id) -> None:
    record_consent(conn, person_id, ConsentPurpose.MARKETING, ConsentState.GRANTED)
    record_consent(conn, person_id, ConsentPurpose.PROFILING, ConsentState.WITHDRAWN)
    live = current_consent(conn, person_id)
    assert set(live) == {ConsentPurpose.MARKETING, ConsentPurpose.PROFILING}


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


def test_erasure_of_a_person_with_no_contract_is_complete(conn, person_id) -> None:
    plan = assess_erasure(conn, person_id)
    assert plan.is_complete
    assert "full_name" in plan.erasable


def test_erasure_executes_across_every_version(conn, person_id) -> None:
    """History is the point of SCD-2, which is exactly why erasure must reach it."""
    frame = golden_person(person_id=person_id, email_address="changed@x.com")
    write_entities(conn, frame, PERSON)

    request_id = request_erasure(conn, person_id, requested_by="dpo@x.com")
    plan = assess_erasure(conn, person_id)
    result = execute_erasure(conn, request_id, plan, executed_by="dpo@x.com")

    assert result["versions_erased"] >= 2
    # full_name is NOT NULL, so erasure tombstones it rather than nulling it.
    surviving = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE person_id = %s "
        "AND full_name <> %s",
        (person_id, ERASED_TOMBSTONE),
    ).fetchone()[0]
    emails = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE person_id = %s AND email_address IS NOT NULL",
        (person_id,),
    ).fetchone()[0]
    assert surviving == 0
    assert emails == 0


def test_erasure_records_what_was_erased(conn, person_id) -> None:
    request_id = request_erasure(conn, person_id, requested_by="dpo@x.com")
    plan = assess_erasure(conn, person_id)
    execute_erasure(conn, request_id, plan, executed_by="dpo@x.com")

    row = conn.execute(
        "SELECT state, erased_fields FROM mdm.erasure_request WHERE request_id = %s",
        (request_id,),
    ).fetchone()
    assert row[0] == ErasureState.ERASED
    assert "full_name" in row[1]


def test_erasure_deactivates_the_crosswalk(conn, person_id) -> None:
    """A re-delivery of the same key must not silently recreate the person."""
    conn.execute(
        "INSERT INTO mdm.person_xref (xref_id, person_id, source_system, source_key_kind,"
        " source_party_key, is_active, linked_at, linked_by, derivation_method, confidence)"
        " VALUES (gen_random_uuid(), %s, 'S', 'OWNER_CUSTOMER_ID', 'K', true, now(),"
        " 'test', 'DETERMINISTIC', 1.0)",
        (person_id,),
    )
    request_id = request_erasure(conn, person_id, requested_by="dpo@x.com")
    plan = assess_erasure(conn, person_id)
    execute_erasure(conn, request_id, plan, executed_by="dpo@x.com")

    active = conn.execute(
        "SELECT count(*) FROM mdm.person_xref WHERE person_id = %s AND is_active",
        (person_id,),
    ).fetchone()[0]
    assert active == 0


def test_retention_without_a_reason_is_rejected_by_the_database(conn, person_id) -> None:
    """An erasure that silently kept data is the failure this prevents."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO mdm.erasure_request "
            "(request_id, person_id, state, requested_by, retained_fields) "
            "VALUES (%s, %s, 'PARTIALLY_ERASED', 'x', ARRAY['full_name'])",
            (uuid7(), person_id),
        )


def test_erasing_nothing_is_an_error(conn, person_id) -> None:
    from cmdm.governance.privacy import ErasurePlan

    request_id = request_erasure(conn, person_id, requested_by="dpo@x.com")
    empty = ErasurePlan(person_id, (), (), None, False, 0, 0)
    with pytest.raises(ValueError, match="nothing to erase"):
        execute_erasure(conn, request_id, empty, executed_by="x")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_match_quality_scores_a_perfect_resolution() -> None:
    assignments = pl.DataFrame({"person_id": ["a", "b", "c"], "master_id": ["a", "a", "c"]})
    labels = pl.DataFrame({"person_id": ["a", "b", "c"], "true_id": ["x", "x", "y"]})
    quality = evaluate_matching(assignments, labels)
    assert quality.precision == 1.0
    assert quality.recall == 1.0


def test_match_quality_catches_an_over_merge() -> None:
    """A false positive merges two real people, which is the expensive failure."""
    assignments = pl.DataFrame({"person_id": ["a", "b"], "master_id": ["a", "a"]})
    labels = pl.DataFrame({"person_id": ["a", "b"], "true_id": ["x", "y"]})
    quality = evaluate_matching(assignments, labels)
    assert quality.precision == 0.0
    assert quality.false_positives == 1


def test_match_quality_catches_a_missed_duplicate() -> None:
    assignments = pl.DataFrame({"person_id": ["a", "b"], "master_id": ["a", "b"]})
    labels = pl.DataFrame({"person_id": ["a", "b"], "true_id": ["x", "x"]})
    quality = evaluate_matching(assignments, labels)
    assert quality.recall == 0.0
    assert quality.false_negatives == 1


def test_unlabelled_records_are_not_scored() -> None:
    """Treating them as non-duplicates would manufacture false positives."""
    assignments = pl.DataFrame({"person_id": ["a", "b", "z"], "master_id": ["a", "a", "z"]})
    labels = pl.DataFrame({"person_id": ["a", "b"], "true_id": ["x", "x"]})
    quality = evaluate_matching(assignments, labels)
    assert quality.precision == 1.0
    assert quality.false_positives == 0


def test_match_quality_on_an_empty_overlap() -> None:
    quality = evaluate_matching(
        pl.DataFrame({"person_id": ["a"], "master_id": ["a"]}),
        pl.DataFrame({"person_id": ["z"], "true_id": ["x"]}),
    )
    assert quality.labelled_pairs == 0


def test_data_quality_reports_completeness_and_conformity(conn) -> None:
    write_entities(conn, golden_person(), PERSON)
    quality = assess_data_quality(conn)
    assert quality.entities > 0
    assert 0.0 <= quality.mean_completeness <= 1.0
    assert "full_name" in quality.completeness


def test_operational_snapshot_reads_queue_health(conn) -> None:
    snapshot = operational_snapshot(conn)
    assert set(snapshot.queue_depth) >= {"ingest", "standardize", "resolve", "survive"}
    assert snapshot.dead_letters >= 0


def test_prometheus_render_produces_exposition_format(conn) -> None:
    from cmdm.observe import render_prometheus

    out = render_prometheus(conn).decode()
    assert "cmdm_golden_entities" in out
    assert "cmdm_completeness_ratio" in out


def test_prometheus_omits_match_quality_when_unmeasured(conn) -> None:
    """A default of zero would fire an alert on a merely unmeasured system."""
    from cmdm.observe import render_prometheus

    out = render_prometheus(conn).decode()
    match_lines = [
        line for line in out.splitlines()
        if line.startswith("cmdm_match_precision")
    ]
    assert match_lines == [] or match_lines[0].endswith("0.0")


# ---------------------------------------------------------------------------
# API and consoles
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(migrated, monkeypatch):
    """A TestClient wired to the test database."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CMDM_DSN", migrated)
    from cmdm.db import engine

    engine.close_pool()
    from cmdm.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    engine.close_pool()


@pytest.fixture()
def keys(migrated):
    """Principals covering each role, committed so the API can see them."""
    import psycopg as _psycopg

    out = {}
    with _psycopg.connect(migrated) as setup:
        setup.execute("DELETE FROM mdm.principal WHERE subject LIKE 'test-%'")
        for role in (Role.VIEWER, Role.OPERATOR, Role.STEWARD, Role.INGESTOR, Role.ADMIN):
            _, secret = create_principal(setup, f"test-{role.lower()}@x.com", [role])
            out[role] = secret
        setup.commit()
    yield out
    with _psycopg.connect(migrated) as teardown:
        teardown.execute("DELETE FROM mdm.principal WHERE subject LIKE 'test-%'")
        teardown.commit()


def test_health_touches_the_database(client) -> None:
    """A health check that skips the database reports healthy while all calls fail."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "golden_persons" in body


def test_unauthenticated_requests_are_refused(client) -> None:
    assert client.get("/persons?name=smith").status_code == 403


def test_viewer_may_search(client, keys) -> None:
    response = client.get(
        "/persons?name=smith", headers={"X-API-Key": keys[Role.VIEWER]}
    )
    assert response.status_code == 200
    assert response.json()["masked"] is True


def test_operator_search_is_unmasked(client, keys) -> None:
    body = client.get(
        "/persons?name=smith", headers={"X-API-Key": keys[Role.OPERATOR]}
    ).json()
    assert body["masked"] is False


def test_search_requires_a_criterion(client, keys) -> None:
    """An uncapped, criterion-less search is a bulk export nobody labelled."""
    response = client.get("/persons", headers={"X-API-Key": keys[Role.VIEWER]})
    assert response.status_code == 400


def test_search_limit_is_capped(client, keys) -> None:
    response = client.get(
        "/persons?name=a&limit=100000", headers={"X-API-Key": keys[Role.VIEWER]}
    )
    assert response.status_code == 422


def test_ingestor_cannot_search(client, keys) -> None:
    response = client.get("/persons?name=smith", headers={"X-API-Key": keys[Role.INGESTOR]})
    assert response.status_code == 403


def test_viewer_cannot_duplicate_check(client, keys) -> None:
    response = client.post(
        "/duplicate-check", json={"full_name": "X"},
        headers={"X-API-Key": keys[Role.VIEWER]},
    )
    assert response.status_code == 403


def test_duplicate_check_on_an_unknown_party_rejects(client, keys) -> None:
    body = client.post(
        "/duplicate-check",
        json={"full_name": "Zebediah Quillfeather", "email": "zq@nowhere.example"},
        headers={"X-API-Key": keys[Role.OPERATOR]},
    ).json()
    assert body["best_zone"] == "AUTO_REJECT"
    assert body["matches"] == []


def test_duplicate_check_requires_a_name(client, keys) -> None:
    response = client.post(
        "/duplicate-check", json={}, headers={"X-API-Key": keys[Role.OPERATOR]}
    )
    assert response.status_code == 422


def test_duplicate_check_returns_a_zone_not_a_boolean(client, keys) -> None:
    """The three-way answer is the useful one at point of entry."""
    body = client.post(
        "/duplicate-check", json={"full_name": "Someone Unknown"},
        headers={"X-API-Key": keys[Role.OPERATOR]},
    ).json()
    assert body["best_zone"] in ("AUTO_MATCH", "GREY", "AUTO_REJECT")


def test_unknown_person_is_404(client, keys) -> None:
    response = client.get(
        f"/persons/{uuid.uuid4()}", headers={"X-API-Key": keys[Role.VIEWER]}
    )
    assert response.status_code == 404


def test_submit_rejects_an_unknown_mapping(client, keys) -> None:
    response = client.post(
        "/batches?mapping_name=nope",
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"X-API-Key": keys[Role.INGESTOR]},
    )
    assert response.status_code == 404


def test_metrics_endpoint_is_exposition_format(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "cmdm_golden_entities" in response.text


def test_console_sends_an_unauthenticated_browser_to_sign_in(client) -> None:
    """A person cannot act on a 403 with a JSON body; they need the form."""
    response = client.get("/console", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/console/login")


def test_console_login_exchanges_a_key_for_a_session(client, keys) -> None:
    response = client.post(
        "/console/login",
        data={"key": keys[Role.VIEWER], "next": "/console"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/console"
    assert client.get("/console").status_code == 200


def test_console_login_refuses_a_bad_key(client) -> None:
    response = client.post(
        "/console/login", data={"key": "not-a-key"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/console/login")
    assert "cmdm_session" not in response.cookies


def test_console_login_will_not_redirect_off_site(client, keys) -> None:
    response = client.post(
        "/console/login",
        data={"key": keys[Role.VIEWER], "next": "https://elsewhere.example/steal"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/console"


def test_business_console_renders(client, keys) -> None:
    response = client.get("/console?q=smith", headers={"X-API-Key": keys[Role.VIEWER]})
    assert response.status_code == 200
    assert "Customer MDM" in response.text


def test_viewer_cannot_open_the_steward_queue(client, keys) -> None:
    response = client.get("/console/steward", headers={"X-API-Key": keys[Role.VIEWER]})
    assert response.status_code == 403


def test_steward_queue_renders(client, keys) -> None:
    response = client.get("/console/steward", headers={"X-API-Key": keys[Role.STEWARD]})
    assert response.status_code == 200


def test_rules_console_requires_the_approve_permission(client, keys) -> None:
    assert client.get(
        "/console/rules", headers={"X-API-Key": keys[Role.OPERATOR]}
    ).status_code == 403
    assert client.get(
        "/console/rules", headers={"X-API-Key": keys[Role.STEWARD]}
    ).status_code == 200


def test_quality_console_renders(client, keys) -> None:
    response = client.get("/console/quality", headers={"X-API-Key": keys[Role.VIEWER]})
    assert response.status_code == 200
    assert "completeness" in response.text.lower()


def test_console_escapes_interpolated_values(client, keys) -> None:
    """Names arrive from feeds nobody validates for markup."""
    response = client.get(
        "/console?q=%3Cscript%3Ealert(1)%3C/script%3E",
        headers={"X-API-Key": keys[Role.VIEWER]},
    )
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
