"""Households and affiliations: who a party lives with, and who they belong to.

The canonical model has always declared PARTY_PARTY edges, an association
vocabulary and a HOUSEHOLD_MEMBER value, and nothing derived any of them. This
is the pass that does, and it answers two questions that look similar and are
not:

*   **Household** -- the parties a person lives with as a family. Built only
    from relations a source *stated*: insurable interest is a condition of
    issue, so a life administration system records at application that the
    owner is the insured's spouse, parent or child. That statement is evidence;
    a shared postcode is not.
*   **Affiliation** -- the legal entities a person is linked to. A company that
    insures its directors, a family trust that holds the policies, an estate
    that owns the cover on the person who died. These are *not* households and
    are counted separately, because a company insuring forty staff is not a
    household of forty-one, and collapsing the two is the standard way this
    feature produces nonsense.

Why the distinction is enforced rather than assumed
---------------------------------------------------

The tempting implementation is "group people by address". It is wrong in both
directions and the sample data contains both failures on purpose:

*   **Flatmates.** Two unrelated adults at one address, no shared policy, no
    stated relation. Address-grouping puts them in one household; this pass
    does not, because nothing asserted a family relation between them.
*   **Married-in members.** A spouse who kept their own surname. Any rule
    requiring a surname match loses them; this pass keeps them, because the
    source said SPOUSE.

Address is used, but only to *corroborate*: a stated relation between two
parties at different addresses is still a family relation -- an adult child
insuring an elderly parent is the common case -- so address raises confidence
rather than gating membership.

How it runs
-----------

One pass over the whole book, not over the batch. A household is a property of
the population: a batch adding one member changes the household its existing
members are in, so recomputing only the batch would leave the others pointing
at a stale group. The work is a group-by and a single
``connected_components`` call, the same compiled union-find the resolver uses,
so "the whole book" is milliseconds rather than something to avoid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl
import psycopg

from cmdm.model.enums import AssociationType as AT
from cmdm.model.enums import DerivationMethod, EdgeKind, PartyType

__all__ = [
    "household_of",
    "HouseholdReport",
    "STATED_TO_ASSOCIATION",
    "derive_households",
    "load_graph_inputs",
]

#: How a source's vocabulary maps onto ours.
#:
#: Deliberately partial. A value not listed here is carried on the edge as
#: delivered and contributes no household membership -- an unrecognised code is
#: something to look at, not something to guess the meaning of. SELF is present
#: and maps to nothing: it says the owner and the insured are one party, which
#: is a statement about identity, not about a family.
STATED_TO_ASSOCIATION: dict[str, AT | None] = {
    "SELF": None,
    "SPOUSE": AT.SPOUSE_OF,
    "PARTNER": AT.SPOUSE_OF,
    "HUSBAND": AT.SPOUSE_OF,
    "WIFE": AT.SPOUSE_OF,
    "CHILD": AT.CHILD_OF,
    "SON": AT.CHILD_OF,
    "DAUGHTER": AT.CHILD_OF,
    "PARENT": AT.PARENT_OF,
    "MOTHER": AT.PARENT_OF,
    "FATHER": AT.PARENT_OF,
    "EMPLOYER": AT.EMPLOYEE_OF,
    "TRUSTEE": AT.TRUST_MEMBER_OF,
    "EXECUTOR": AT.ESTATE_SUBJECT_OF,
}

#: A household larger than this is reported rather than trusted. A stated
#: relation is strong evidence, but one wrong edge unions two families, and the
#: damage is proportional to the size of both. Twelve is generous for a family
#: and small enough that a data problem stands out.
SUSPICIOUS_HOUSEHOLD = 12


@dataclass(slots=True)
class HouseholdReport:
    """What one householding pass found."""

    parties: int = 0
    associations: int = 0
    households: int = 0
    people_in_a_household: int = 0
    largest_household: int = 0
    multi_person_households: int = 0
    affiliations: int = 0
    affiliated_parties: int = 0
    corroborated_by_address: int = 0
    suspicious: list[tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parties": self.parties,
            "associations": self.associations,
            "households": self.households,
            "people_in_a_household": self.people_in_a_household,
            "largest_household": self.largest_household,
            "multi_person_households": self.multi_person_households,
            "affiliations": self.affiliations,
            "affiliated_parties": self.affiliated_parties,
            "corroborated_by_address": self.corroborated_by_address,
            "suspicious": self.suspicious,
        }


def load_graph_inputs(
    conn: psycopg.Connection,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Read the whole current book: parties, and the sourced edges over them.

    Read from the golden store rather than from the batch being processed,
    because a household spans batches: the spouse whose policy arrived last
    month is in the same family as the one that arrived today.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT person_id::text, party_type::text, address_key,
                   surname_derived, full_name
            FROM mdm.person
            WHERE is_current AND NOT is_deleted
            """
        )
        parties = pl.DataFrame(
            cur.fetchall() or [],
            schema=["person_id", "party_type", "address_key", "surname_derived",
                    "full_name"],
            orient="row",
        )

        cur.execute(
            """
            SELECT from_person_id::text, to_policy_id::text, role::text,
                   stated_relationship
            FROM mdm.relationship
            WHERE is_current AND NOT is_deleted AND edge_kind = 'PARTY_POLICY'
            """
        )
        edges = pl.DataFrame(
            cur.fetchall() or [],
            schema=["person_id", "policy_id", "role", "stated_relationship"],
            orient="row",
        )

    return parties, edges


def _stated_pairs(edges: pl.DataFrame) -> pl.DataFrame:
    """Owner-to-insured pairs carrying the relation the source stated.

    The statement lives on the owner's edge and names a relation to the life
    insured, so the counterparty is found by joining the owner edge to the
    insured edge of the same policy. A policy with no insured, or one whose
    owner and insured resolved to the same party, yields nothing -- the first
    has no counterparty and the second is SELF by another name.
    """
    owners = edges.filter(
        (pl.col("role") == "OWNER") & pl.col("stated_relationship").is_not_null()
    ).select(
        pl.col("person_id").alias("from_id"),
        "policy_id",
        "stated_relationship",
    )
    insureds = edges.filter(pl.col("role") == "INSURED").select(
        pl.col("person_id").alias("to_id"), "policy_id"
    )

    return (
        owners.join(insureds, on="policy_id", how="inner")
        .filter(pl.col("from_id") != pl.col("to_id"))
        .with_columns(
            pl.col("stated_relationship")
            .replace_strict(
                {k: (v.value if v else None) for k, v in STATED_TO_ASSOCIATION.items()},
                default=None,
            )
            .alias("association_type")
        )
        .filter(pl.col("association_type").is_not_null())
    )


def _associations(pairs: pl.DataFrame, parties: pl.DataFrame) -> pl.DataFrame:
    """One row per party pair per association, with its evidence.

    Aggregated across policies: a couple with four joint policies is one
    SPOUSE_OF association evidenced by four, not four associations. The
    evidence is kept because a derived edge that cannot be traced to the facts
    behind it is not auditable, and because two people sharing five policies is
    a much stronger signal than two sharing one.
    """
    if pairs.height == 0:
        return pairs

    address = parties.select(
        pl.col("person_id"), pl.col("address_key")
    )

    return (
        pairs.group_by("from_id", "to_id", "association_type")
        .agg(
            pl.col("policy_id").unique().alias("evidence_policy_ids"),
            pl.col("policy_id").n_unique().alias("evidence_count"),
        )
        .join(address.rename({"person_id": "from_id",
                              "address_key": "from_address"}), on="from_id",
              how="left")
        .join(address.rename({"person_id": "to_id",
                              "address_key": "to_address"}), on="to_id",
              how="left")
        .with_columns(
            (
                pl.col("from_address").is_not_null()
                & (pl.col("from_address") == pl.col("to_address"))
            ).alias("same_address")
        )
    )


def _households(
    associations: pl.DataFrame, parties: pl.DataFrame
) -> tuple[pl.DataFrame, int, list[tuple[str, int]]]:
    """Group parties into households over the familial associations only.

    Connected components, so a family is transitive: if the source says A is
    B's spouse and B is C's parent, all three are one household without anyone
    having to state the A-to-C relation. That transitivity is exactly why the
    input is restricted to *stated* family relations -- run the same closure
    over "shares an address" and one apartment block becomes one household.

    Legal entities are excluded outright. A trust is not a member of the family
    whose policies it holds; it is a thing the family owns, and its link is an
    affiliation.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    natural = parties.filter(pl.col("party_type") == PartyType.PERSON.value)
    ids = natural["person_id"].unique().sort()
    n = ids.len()
    if n == 0:
        empty = pl.DataFrame(
            schema={"person_id": pl.String, "household_id": pl.String,
                    "household_size": pl.Int32}
        )
        return empty, 0, []

    index = pl.DataFrame({"person_id": ids}).with_row_index("idx")
    familial = {a.value for a in AT.familial()}
    family_edges = associations.filter(
        pl.col("association_type").is_in(list(familial))
    ) if associations.height else associations

    if family_edges.height:
        joined = (
            family_edges.select("from_id", "to_id")
            .join(index.rename({"person_id": "from_id", "idx": "from_idx"}),
                  on="from_id", how="inner")
            .join(index.rename({"person_id": "to_id", "idx": "to_idx"}),
                  on="to_id", how="inner")
        )
        rows = joined["from_idx"].to_numpy()
        cols = joined["to_idx"].to_numpy()
    else:
        rows = np.array([], dtype=np.int64)
        cols = np.array([], dtype=np.int64)

    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n, n)
    ).tocsr()
    _, labels = connected_components(graph, directed=False, return_labels=True)

    assigned = index.with_columns(pl.Series("label", labels))
    groups = assigned.group_by("label").agg(
        pl.col("person_id").min().alias("anchor"),
        pl.len().alias("household_size"),
    )

    # A household of one is not a household. Recording an id for every solitary
    # policyholder would put 1,500 "households" on the dashboard and mean
    # nothing by any of them; the honest answer for a party with no stated
    # relation to anybody is that no household was established.
    groups = groups.with_columns(
        pl.when(pl.col("household_size") > 1)
        .then(
            # Derived from the smallest member id, so the same family gets the
            # same household id on every run, and keeps it when it gains a
            # member. A fresh uuid per run would churn the column nightly.
            pl.col("anchor").map_elements(_household_uuid, return_dtype=pl.String)
        )
        .otherwise(None)
        .alias("household_id")
    )

    result = (
        assigned.join(groups, on="label")
        .select(
            "person_id",
            "household_id",
            pl.when(pl.col("household_id").is_not_null())
            .then(pl.col("household_size"))
            .otherwise(0)
            .cast(pl.Int32)
            .alias("household_size"),
        )
    )

    multi = groups.filter(pl.col("household_id").is_not_null())
    suspicious = [
        (str(r["household_id"]), int(r["household_size"]))
        for r in multi.filter(pl.col("household_size") >= SUSPICIOUS_HOUSEHOLD)
        .sort("household_size", descending=True)
        .iter_rows(named=True)
    ]
    return result, multi.height, suspicious


def _household_uuid(anchor: str) -> str:
    """A stable household id, derived from the household's anchor member.

    UUIDv5 over the anchor rather than a fresh uuid7: the id has to be the same
    on every run for the same family, or the column changes nightly and every
    downstream consumer sees a household that never stops moving. Namespaced so
    it cannot collide with a person id.
    """
    return str(uuid.uuid5(_HOUSEHOLD_NAMESPACE, anchor))


#: Fixed namespace for household ids. A constant, so the ids are reproducible
#: across machines and across a rebuild of the store from the landing zone.
_HOUSEHOLD_NAMESPACE = uuid.UUID("6f1a5b3c-9d24-5e77-8a10-2c4e7b91d0f3")


def derive_households(
    conn: psycopg.Connection, *, write: bool = True
) -> HouseholdReport:
    """Derive party-to-party edges and households over the whole current book.

    Returns what it found. With ``write=False`` it computes and reports without
    touching the store, which is what the console's preview and the tests use.
    """
    parties, edges = load_graph_inputs(conn)
    report = HouseholdReport(parties=parties.height)
    if parties.height == 0:
        return report

    pairs = _stated_pairs(edges)
    associations = _associations(pairs, parties)
    report.associations = associations.height
    if associations.height:
        report.corroborated_by_address = int(associations["same_address"].sum())

    memberships, household_count, suspicious = _households(associations, parties)
    report.households = household_count
    report.suspicious = suspicious
    if memberships.height:
        in_household = memberships.filter(pl.col("household_id").is_not_null())
        report.people_in_a_household = in_household.height
        report.largest_household = int(memberships["household_size"].max() or 0)
        report.multi_person_households = household_count

    affiliation_kinds = {a.value for a in AT.affiliation()}
    affiliations = (
        associations.filter(pl.col("association_type").is_in(list(affiliation_kinds)))
        if associations.height else associations
    )
    report.affiliations = affiliations.height
    if affiliations.height:
        report.affiliated_parties = affiliations["to_id"].n_unique()

    if write:
        _write_party_party(conn, associations)
        _write_membership(conn, memberships, affiliations)

    return report


def _write_party_party(conn: psycopg.Connection, associations: pl.DataFrame) -> int:
    """Persist the derived edges as PARTY_PARTY relationships.

    Written through the same SCD-2 writer as everything else, so a family
    relation that disappears from the source closes a version rather than
    vanishing. Ids are derived from the pair and the association so that a
    re-run recognises the edge it wrote last time; minting fresh ones would
    make every run a full rewrite of the edge table.
    """
    from cmdm.model.fields import RELATIONSHIP
    from cmdm.store import write_entities

    if associations.height == 0:
        return 0

    frame = associations.select(
        pl.col("from_id").alias("from_person_id"),
        pl.col("to_id").alias("to_person_id"),
        pl.lit(EdgeKind.PARTY_PARTY.value).alias("edge_kind"),
        pl.col("association_type"),
        pl.col("evidence_policy_ids"),
        pl.col("evidence_count").cast(pl.Int32),
        pl.lit(DerivationMethod.DETERMINISTIC.value).alias("derivation_method"),
        pl.lit("DERIVED").alias("source_system"),
    ).with_columns(
        pl.struct("from_person_id", "to_person_id", "association_type")
        .map_elements(
            lambda s: str(uuid.uuid5(
                _EDGE_NAMESPACE,
                f"{s['from_person_id']}|{s['to_person_id']}|{s['association_type']}",
            )),
            return_dtype=pl.String,
        )
        .alias("relationship_id")
    )

    return write_entities(conn, frame, RELATIONSHIP).inserted


#: Namespace for derived edge ids, distinct from the household one so that an
#: edge id and a household id can never collide.
_EDGE_NAMESPACE = uuid.UUID("b2c48d16-7e35-5f09-9d63-4a1f8e05c7b2")


def _write_membership(
    conn: psycopg.Connection,
    memberships: pl.DataFrame,
    affiliations: pl.DataFrame,
) -> int:
    """Stamp household and affiliation onto the current Person rows.

    An UPDATE rather than a new SCD-2 version, and deliberately so. These three
    columns are a rollup over the whole population, not an assertion any source
    made about the person: a new customer joining a family would otherwise close
    and reopen a version of every existing member, filling the version history
    with entries whose only content is "somebody else arrived". The record hash
    covers sourced attributes, so this does not disturb change detection either.
    """
    counts = (
        affiliations.group_by("to_id").agg(pl.len().alias("n"))
        if affiliations.height
        else pl.DataFrame(schema={"to_id": pl.String, "n": pl.UInt32})
    )

    rows = (
        memberships.join(
            counts.rename({"to_id": "person_id", "n": "affiliation_count"}),
            on="person_id", how="left",
        )
        .with_columns(pl.col("affiliation_count").fill_null(0).cast(pl.Int32))
    )
    if rows.height == 0:
        return 0

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _household_stage")
        cur.execute(
            """
            CREATE TEMP TABLE _household_stage (
                person_id uuid, household_id uuid,
                household_size integer, affiliation_count integer
            ) ON COMMIT DROP
            """
        )
        with cur.copy(
            "COPY _household_stage (person_id, household_id, household_size, "
            "affiliation_count) FROM STDIN"
        ) as copy:
            for row in rows.iter_rows(named=True):
                copy.write_row((
                    row["person_id"], row["household_id"],
                    int(row["household_size"] or 0),
                    int(row["affiliation_count"] or 0),
                ))

        cur.execute(
            """
            UPDATE mdm.person p
               SET household_id      = s.household_id,
                   household_size    = s.household_size,
                   affiliation_count = s.affiliation_count,
                   updated_at        = now()
              FROM _household_stage s
             WHERE p.person_id = s.person_id
               AND p.is_current
               AND (p.household_id      IS DISTINCT FROM s.household_id
                 OR p.household_size    IS DISTINCT FROM s.household_size
                 OR p.affiliation_count IS DISTINCT FROM s.affiliation_count)
            """
        )
        return cur.rowcount or 0


def household_of(
    conn: psycopg.Connection, person_id: str | uuid.UUID
) -> dict[str, Any]:
    """Everything the store knows about one party's household and affiliations.

    Shaped for a servicing screen: who else is in the household, how each of
    them is related and on what evidence, and which legal entities the party is
    linked to. Returns empty lists rather than raising for a party with neither,
    because "this customer has no household on record" is an answer.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT household_id::text, household_size, affiliation_count "
            "FROM mdm.person WHERE person_id = %s AND is_current",
            (str(person_id),),
        )
        row = cur.fetchone()
        if row is None:
            return {"household_id": None, "members": [], "affiliations": [],
                    "relations": []}
        household_id, size, affiliations = row

        members: list[dict[str, Any]] = []
        if household_id:
            cur.execute(
                """
                SELECT person_id::text, full_name, party_type::text,
                       date_of_birth, address_key
                FROM mdm.person
                WHERE household_id = %s AND is_current AND NOT is_deleted
                ORDER BY date_of_birth NULLS LAST
                """,
                (household_id,),
            )
            members = [
                {"person_id": r[0], "full_name": r[1], "party_type": r[2],
                 "date_of_birth": r[3], "address_key": r[4]}
                for r in cur.fetchall()
            ]

        # Every derived edge touching this party, in both directions: the edge
        # is stored once, from the party the source described.
        cur.execute(
            """
            SELECT r.association_type::text, r.evidence_count,
                   other.person_id::text, other.full_name, other.party_type::text,
                   (r.from_person_id = %(id)s) AS outgoing
            FROM mdm.relationship r
            JOIN mdm.person other
              ON other.person_id = CASE WHEN r.from_person_id = %(id)s
                                        THEN r.to_person_id ELSE r.from_person_id END
             AND other.is_current
            WHERE r.edge_kind = 'PARTY_PARTY' AND r.is_current
              AND (r.from_person_id = %(id)s OR r.to_person_id = %(id)s)
            ORDER BY r.evidence_count DESC
            """,
            {"id": str(person_id)},
        )
        relations = [
            {"association_type": r[0], "evidence_count": r[1], "person_id": r[2],
             "full_name": r[3], "party_type": r[4], "outgoing": r[5]}
            for r in cur.fetchall()
        ]

    affiliation_kinds = {a.value for a in AT.affiliation()}
    return {
        "household_id": household_id,
        "household_size": size,
        "affiliation_count": affiliations,
        "members": members,
        "relations": [r for r in relations
                      if r["association_type"] not in affiliation_kinds],
        "affiliations": [r for r in relations
                         if r["association_type"] in affiliation_kinds],
    }
