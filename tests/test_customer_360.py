"""The assembled single-customer view.

Every panel on that page is a claim about what a party is connected to, and the
claims are the sort that look right while being wrong: a relationship rendered
from the wrong end, a policy counted twice, an agent's book added to a
customer's exposure. None of those raise an error. They just quietly say
something false about a person's insurance.

So the tests here are mostly about direction, grouping and what is deliberately
*excluded* -- the three things a reviewer cannot check by looking at the page,
because a wrong answer renders exactly as convincingly as a right one.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest

from cmdm.ui.data import ROLE_GROUPS, load_customer_360, relation_label

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sample() -> pl.DataFrame:
    return pl.read_csv(
        REPO / "data" / "life_admin_sample.csv", infer_schema_length=None,
        n_rows=1200,
    )


@pytest.fixture
def store(conn, sample):
    """A processed store, so the view is assembled over real derived data."""
    from cmdm.ingest.landing import accept_batch
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import process_batch

    conn.execute(
        "TRUNCATE mdm.source_record, mdm.relationship, mdm.person, mdm.policy, "
        "mdm.person_master, mdm.policy_master, mdm.person_xref, "
        "mdm.policy_xref, mdm.attribute_provenance, mdm.match_pair CASCADE"
    )
    mapping = load_mapping(REPO / "src" / "cmdm" / "mappings" / "life_admin.toml")
    batch_id, report, _ = accept_batch(
        conn, sample, mapping, origin="T360", filename="s.csv", submitted_by="pytest",
    )
    assert report.accepted
    process_batch(conn, batch_id)
    return conn


def _person_with_policies(conn, minimum: int = 3):
    return conn.execute(
        """
        SELECT r.from_person_id
        FROM mdm.relationship r
        JOIN mdm.person p ON p.person_id = r.from_person_id AND p.is_current
        WHERE r.edge_kind = 'PARTY_POLICY' AND p.party_type = 'PERSON'
        GROUP BY 1 HAVING count(*) >= %s
        ORDER BY count(*) DESC LIMIT 1
        """,
        (minimum,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# It loads at all
# ---------------------------------------------------------------------------


def test_an_unknown_party_is_none_not_an_exception(store) -> None:
    """A stale link is a 404, and 404 is a page. Raising here would make every
    caller write the same try/except to render one."""
    import uuid

    assert load_customer_360(store, uuid.uuid4()) is None


def test_the_view_carries_the_golden_record(store) -> None:
    found = load_customer_360(store, _person_with_policies(store))
    assert found is not None
    assert found.record["full_name"]
    assert found.record["is_current"] is True


# ---------------------------------------------------------------------------
# Linkage: the reason the page exists
# ---------------------------------------------------------------------------


def test_every_policy_carries_the_other_parties_on_it(store) -> None:
    """The panel's whole purpose. "Five policies" is a fact about a row; "two of
    them are cover on his son" is what somebody rang up to find out."""
    found = load_customer_360(store, _person_with_policies(store))

    assert found.policies, "picked a party with policies, got none back"
    assert any(p.counterparties for p in found.policies), (
        "no policy came back with another party on it, which cannot be true of "
        "a book where every contract has an owner, an insured and an agent"
    )
    for policy in found.policies:
        for other in policy.counterparties:
            assert other["person_id"] != found.person_id, (
                "the party being viewed is listed as its own counterparty"
            )


def test_counterparties_come_from_the_same_policy(store) -> None:
    """The join is per policy. Getting it wrong -- collecting every party on
    every policy and showing them all on each -- produces a page that is
    plausible, busy and completely false."""
    found = load_customer_360(store, _person_with_policies(store))

    for policy in found.policies:
        rows = store.execute(
            """
            SELECT count(*) FROM mdm.relationship
            WHERE edge_kind = 'PARTY_POLICY' AND to_policy_id = %s
              AND from_person_id <> %s AND is_current
            """,
            (policy.policy_id, found.person_id),
        ).fetchone()[0]
        assert len(policy.counterparties) == rows


def test_roles_are_grouped_by_what_the_party_is_on_the_policy(store) -> None:
    found = load_customer_360(store, _person_with_policies(store))

    for policy in found.policies:
        assert policy.role_group in ROLE_GROUPS
        assert policy.role in ROLE_GROUPS[policy.role_group]


def test_an_agents_book_is_not_counted_as_their_own_cover(store) -> None:
    """The number this page could most easily get catastrophically wrong. An
    agent servicing 200 policies is not exposed to 200 policies' sum assured,
    and a headline figure saying so would be read as exposure by whoever it was
    put in front of."""
    agent = store.execute(
        """
        SELECT r.from_person_id
        FROM mdm.relationship r
        WHERE r.edge_kind = 'PARTY_POLICY'
          AND r.role IN ('AGENT', 'WRITING_AGENT', 'SERVICING_AGENT')
          AND r.is_current
        GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
        """
    ).fetchone()[0]

    found = load_customer_360(store, agent)
    assert found.by_group("services"), "expected an agent with a book"
    assert found.total_sum_assured == 0.0, (
        "the agent's book was added to their own cover"
    )


def test_a_household_member_is_labelled_from_this_partys_side(store) -> None:
    """A stored CHILD_OF edge means one thing read forwards and the opposite
    read backwards. Rendering the stored direction regardless of who is being
    viewed makes the page state that a woman is her own daughter's child."""
    row = store.execute(
        """
        SELECT r.from_person_id, r.to_person_id
        FROM mdm.relationship r
        WHERE r.edge_kind = 'PARTY_PARTY' AND r.association_type = 'CHILD_OF'
          AND r.is_current
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("no CHILD_OF edge in this slice of the extract")

    child, parent = row
    from_child = load_customer_360(store, child)
    from_parent = load_customer_360(store, parent)

    child_says = {m["person_id"]: m["relation"] for m in from_child.household_members}
    parent_says = {m["person_id"]: m["relation"] for m in from_parent.household_members}

    assert child_says.get(str(parent)) == "child of"
    assert parent_says.get(str(child)) == "parent of"


def test_a_party_is_never_its_own_household_member(store) -> None:
    """It was, once: two source key namespaces split one customer into two
    golden records, and the page showed a woman as a member of her own
    household."""
    found = load_customer_360(store, _person_with_policies(store))
    assert all(
        m["person_id"] != found.person_id for m in found.household_members
    )


def test_affiliations_are_kept_out_of_the_household(store) -> None:
    """An employer is not family. Merging the two would be the single most
    misleading thing this page could do, and it is one join away."""
    org = store.execute(
        """
        SELECT r.from_person_id
        FROM mdm.relationship r
        WHERE r.edge_kind = 'PARTY_PARTY'
          AND r.association_type IN ('EMPLOYEE_OF', 'TRUST_MEMBER_OF',
                                     'ESTATE_SUBJECT_OF')
          AND r.is_current
        LIMIT 1
        """
    ).fetchone()
    if org is None:
        pytest.skip("no affiliation edge in this slice of the extract")

    found = load_customer_360(store, org[0])
    household = {m["person_id"] for m in found.household_members}
    assert not household & {a["person_id"] for a in found.affiliations}


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------


def test_the_crosswalk_is_shown_not_the_record_id(store) -> None:
    """A golden record has no natural key. The source keys are the only thing an
    operator can reconcile against the system the data came from."""
    found = load_customer_360(store, _person_with_policies(store))
    assert found.sources
    assert all(s["source_party_key"] for s in found.sources)


def test_rejected_pairs_are_kept_alongside_accepted_ones(store) -> None:
    """"Why are these two *not* the same person" is asked as often as the
    opposite, and the golden record has no memory of a party it was decided not
    to be."""
    person = store.execute(
        """
        SELECT left_person_id FROM mdm.match_pair
        WHERE final_decision = 'NO_MATCH' AND left_person_id IS NOT NULL
        LIMIT 1
        """
    ).fetchone()
    if person is None:
        pytest.skip("no scored pairs in this slice of the extract")

    found = load_customer_360(store, person[0])
    assert any(c["final_decision"] == "NO_MATCH" for c in found.considered)


def test_the_match_ledger_names_the_other_party_readably(store) -> None:
    """The stored identity is `system\\x1fkind\\x1fkey`. Rendered raw it shows a
    control character where the separator is, and the key -- the only part an
    operator recognises -- is buried in it."""
    person = store.execute(
        "SELECT left_person_id FROM mdm.match_pair "
        "WHERE left_person_id IS NOT NULL LIMIT 1"
    ).fetchone()
    if person is None:
        pytest.skip("no scored pairs in this slice of the extract")

    found = load_customer_360(store, person[0])
    for entry in found.considered:
        assert "\x1f" not in entry["left_source_identity"]
        assert "\x1f" not in entry["right_source_identity"]


def test_a_merged_id_still_resolves(store) -> None:
    """Published ids are quoted in other systems. One that stopped resolving
    after a merge would break every link anybody saved."""
    merged = store.execute(
        """
        SELECT pm.person_id, pm.merged_into_id
        FROM mdm.person_master pm
        WHERE pm.merged_into_id IS NOT NULL LIMIT 1
        """
    ).fetchone()
    if merged is None:
        pytest.skip("nothing was merged in this slice of the extract")

    found = load_customer_360(store, merged[0])
    assert found is not None
    assert found.person_id == str(merged[1])
    assert found.was_merged_id, "the page would not tell anyone it had redirected"


# ---------------------------------------------------------------------------
# Direction labelling, on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("association", "outgoing", "expected"),
    [
        ("CHILD_OF", True, "child of"),
        ("CHILD_OF", False, "parent of"),
        ("PARENT_OF", True, "parent of"),
        ("PARENT_OF", False, "child of"),
        ("SPOUSE_OF", True, "spouse of"),
        ("SPOUSE_OF", False, "spouse of"),
        ("EMPLOYEE_OF", True, "employee of"),
        ("EMPLOYEE_OF", False, "employs"),
    ],
)
def test_relationship_direction(association, outgoing, expected) -> None:
    assert relation_label(association, outgoing) == expected


def test_an_unmapped_association_still_reads_as_words() -> None:
    """A new association type must not surface as a raw enum. It is a label a
    customer-facing operator reads out loud."""
    assert relation_label("SOMETHING_NEW", True) == "something new"
