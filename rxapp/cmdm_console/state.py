"""State: what the browser holds, and how it is filled from the store.

Reflex ships state to the client and back over a websocket, which makes two
things non-negotiable in a system holding insurance customer data.

**Masking happens here, not in the view.** A Reflex var that reaches a component
has already been serialised to the browser, so "render it masked" is not a
control -- the value is on the client whether or not it is drawn. Every string
in this module is masked before it becomes a var, so an unentitled operator's
browser never receives the value at all. This is the one place where the port
from server-rendered HTML changes a security property, and it changes it in the
direction that needed watching.

**Access is logged where it is authorized.** The same `log_access` the API and
the console use, on the same actions, so a read through this UI is
indistinguishable in the audit log from a read through any other -- which is the
only way the log is worth keeping.

Vars are plain strings and lists of string-keyed dicts. Reflex can carry richer
types, but a `Decimal` or a `date` crossing the wire acquires a rendering that
depends on the client's locale, and a policy's sum assured is not a value to let
a browser reformat. Everything is formatted server-side, once.
"""

from __future__ import annotations

from typing import Any

import reflex as rx

from cmdm.governance.rbac import Action, Principal, authenticate, log_access

MASK = "••••"

#: How many customers a search returns. Large enough to be useful, small enough
#: that no single query can push a megabyte of names into a browser.
SEARCH_LIMIT = 50


def _conn():
    """A pooled connection, committed on exit.

    Reflex event handlers are not FastAPI requests, so the dependency that
    manages this for the API does not apply. The pool is the same one, though --
    two connection pools against one embedded PostgreSQL would compete for the
    same small connection budget.
    """
    from cmdm.db.engine import connect

    return connect()


def _fmt_money(value: Any, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{currency or ''} {float(value):,.0f}".strip()


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


class AuthState(rx.State):
    """Who is signed in, and what the rail is allowed to show them."""

    api_key: str = ""
    subject: str = "not signed in"
    roles_label: str = ""
    #: Action names this principal holds. The rail reads it to decide which
    #: destinations exist; a page reads it again before loading anything,
    #: because a hidden link is a courtesy and not an authorization check.
    allowed: list[str] = []
    error: str = ""

    @rx.var
    def active_route(self) -> str:
        return self.router.url.path

    @rx.var
    def signed_in(self) -> bool:
        return bool(self.allowed)

    def principal(self, conn) -> Principal:
        return authenticate(conn, self.api_key or None)

    @rx.event
    def sign_in(self, form: dict):
        key = (form.get("api_key") or "").strip()
        with _conn() as conn:
            principal = authenticate(conn, key or None)
            conn.commit()
        if not principal.roles:
            self.error = "That key was not recognised, or it has been revoked."
            return
        self.api_key = key
        self.subject = principal.subject
        self.roles_label = ", ".join(principal.roles)
        self.allowed = sorted(
            action for action in vars(Action).values()
            if isinstance(action, str) and not action.startswith("_")
            and principal.may(action)
        )
        self.error = ""
        return rx.redirect("/")

    @rx.event
    def sign_out(self):
        self.reset()
        return rx.redirect("/login")


class CustomerState(AuthState):
    """Search results and the assembled 360 for one party.

    Inherits AuthState rather than holding a reference to it so the principal
    and the data it authorizes cannot drift apart across an event: there is one
    state object, and the key that loaded a record is the key that was checked
    for it.
    """

    query: str = ""
    results: list[dict[str, str]] = []
    searched: bool = False

    # --- the 360 ---------------------------------------------------------
    loading: bool = False
    not_found: bool = False
    denied: bool = False
    merged_notice: str = ""

    person_id: str = ""
    name: str = ""
    party_type: str = ""
    attributes: list[dict[str, str]] = []
    flags: list[dict[str, str]] = []

    policy_groups: list[dict[str, str]] = []
    policies: list[dict[str, str]] = []
    household_id: str = ""
    household: list[dict[str, str]] = []
    affiliations: list[dict[str, str]] = []
    sources: list[dict[str, str]] = []
    lineage: list[dict[str, str]] = []
    versions: list[dict[str, str]] = []
    considered: list[dict[str, str]] = []

    stat_policies: str = "0"
    stat_cover: str = "—"
    stat_household: str = "0"
    stat_sources: str = "0"
    stat_contested: str = "0"

    @rx.event
    def search(self, form: dict):
        self.query = (form.get("query") or "").strip()
        self.searched = True
        if not self.query:
            self.results = []
            return
        with _conn() as conn:
            principal = self.principal(conn)
            if not principal.may(Action.SEARCH):
                self.results = []
                self.denied = True
                return
            reveal = principal.may_unmask
            rows = conn.execute(
                """
                SELECT person_id::text, full_name, party_type::text,
                       date_of_birth::text, email_address, source_count
                FROM mdm.person
                WHERE is_current AND full_name_normalized ILIKE %s
                ORDER BY source_count DESC, full_name
                LIMIT %s
                """,
                (f"%{self.query.lower()}%", SEARCH_LIMIT),
            ).fetchall()
            log_access(conn, principal, Action.SEARCH, entity_name="Person")
            conn.commit()

        self.results = [
            {
                "person_id": r[0],
                "full_name": _fmt(r[1]),
                "party_type": _fmt(r[2]),
                "date_of_birth": _fmt(r[3]) if reveal else MASK,
                "email_address": _fmt(r[4]) if reveal else MASK,
                "source_count": str(r[5] or 0),
            }
            for r in rows
        ]

    @rx.event
    def load_customer(self):
        """Fill every panel for the party named in the route."""
        from cmdm.ui.data import load_customer_360

        self.loading = True
        self.not_found = self.denied = False
        self.merged_notice = ""
        # The route arg is deliberately not called `person_id`: Reflex creates a
        # state var per dynamic arg, and that one would shadow the resolved id
        # this page sets after following any merge pointer. The var it does
        # create is read directly rather than out of the params dict, which is
        # the interface Reflex is retiring.
        requested = getattr(self, "customer_id", "")

        try:
            with _conn() as conn:
                principal = self.principal(conn)
                if not principal.may(Action.READ):
                    self.denied, self.loading = True, False
                    return
                reveal = principal.may_unmask

                found = load_customer_360(conn, requested)
                if found is None:
                    self.not_found, self.loading = True, False
                    return

                log_access(conn, principal, Action.READ, entity_name="Person",
                           entity_id=found.person_id, pii_revealed=reveal)
                conn.commit()
        except Exception:
            # A malformed id in the URL is a 404, not a stack trace. Anything
            # else is still not something to render half a page for.
            self.not_found, self.loading = True, False
            return

        self._apply(found, reveal)
        self.loading = False

    def _apply(self, found, reveal: bool) -> None:
        record = found.record
        self.person_id = found.person_id
        self.name = _fmt(record.get("full_name")) if reveal else MASK
        self.party_type = _fmt(record.get("party_type"))

        if found.was_merged_id:
            self.merged_notice = (
                f"The id you followed was merged into {found.person_id}. "
                "Published ids keep resolving, so a link saved months ago still "
                "lands on the surviving record."
            )

        pii = {"date_of_birth", "email_address", "phone_e164",
               "address_normalized", "postal_code"}
        self.attributes = [
            {"key": key.replace("_", " "),
             "value": _fmt(record.get(key)) if (reveal or key not in pii) else MASK}
            for key in (
                "party_type", "date_of_birth", "email_address", "phone_e164",
                "address_normalized", "postal_code", "country_code",
                "occupation", "source_count", "confidence",
            )
            if key in record
        ]

        flags = []
        if record.get("do_not_contact"):
            flags.append({"label": "do not contact", "tone": "var(--bad)"})
        if record.get("is_deceased"):
            flags.append({"label": "deceased", "tone": "var(--muted)"})
        if found.was_merged_id:
            flags.append({"label": "merged id", "tone": "var(--warn)"})
        self.flags = flags

        # A party is routinely both owner and insured on the same contract, so
        # that policy appears under two headings. Without saying so the page
        # looks like it is double-counting; naming the other role turns an
        # apparent duplicate into the fact it actually is.
        roles_on: dict[str, list[str]] = {}
        for p in found.policies:
            roles_on.setdefault(p.policy_id, []).append(p.role)

        self.policies = [
            {
                "policy_id": p.policy_id,
                "also": ", ".join(
                    r.replace("_", " ").lower()
                    for r in roles_on[p.policy_id] if r != p.role
                ),
                "policy_number": p.policy_number,
                "role": p.role.replace("_", " ").lower(),
                "group": p.role_group,
                "product": _fmt(p.product_name),
                "line": _fmt(p.product_line),
                "status": _fmt(p.policy_status),
                "cover": _fmt_money(p.sum_assured_amount, p.currency_code),
                "premium": _fmt_money(p.annual_premium_amount, p.currency_code),
                "effective": _fmt(p.effective_date),
                "system": p.source_system,
                # The whole point of the panel: who else is on this contract.
                "with_whom": ", ".join(
                    f"{(c['full_name'] if reveal else MASK)}"
                    f" ({c['role'].replace('_', ' ').lower()}"
                    + (f", {c['stated_relationship'].lower()}"
                       if c.get("stated_relationship") else "")
                    + ")"
                    for c in p.counterparties
                ) or "no other party on this policy",
            }
            for p in found.policies
        ]
        self.policy_groups = [
            {"group": group,
             "label": _GROUP_LABEL[group],
             "hint": _GROUP_HINT[group],
             "count": str(len(found.by_group(group)))}
            for group in found.groups_present
        ]

        self.household_id = found.household_id or ""
        self.household = [
            {"person_id": m["person_id"],
             "name": _fmt(m["full_name"]) if reveal else MASK,
             "relation": m["relation"],
             "date_of_birth": _fmt(m.get("date_of_birth")) if reveal else MASK,
             "evidence": (f"{m['evidence_count']} policy"
                          if m["evidence_count"] == 1
                          else f"{m['evidence_count']} policies")
                         if m["stated"] else "same household, no direct edge"}
            for m in found.household_members
        ]
        self.affiliations = [
            {"person_id": a["person_id"],
             "name": _fmt(a["full_name"]),
             "party_type": _fmt(a.get("party_type")),
             "relation": a["relation"],
             "evidence": str(a.get("evidence_count", 0))}
            for a in found.affiliations
        ]

        self.sources = [
            {"system": _fmt(s["source_system"]),
             "kind": _fmt(s["source_key_kind"]),
             "key": _fmt(s["source_party_key"]) if reveal else MASK,
             "active": "yes" if s["is_active"] else "no",
             "how": _fmt(s["derivation_method"]),
             "confidence": f"{float(s['confidence'] or 0):.2f}"}
            for s in found.sources
        ]

        self.lineage = [
            {"attribute": _fmt(entry["attribute_name"]),
             "value": (_fmt(entry["value_text"]) if reveal else MASK),
             "strategy": _fmt(entry["strategy"]),
             "won_from": _fmt(entry["winning_source_system"]),
             "candidates": str(entry["candidate_count"])}
            for entry in found.lineage
        ]

        self.versions = [
            {"version": str(v["version"]),
             "valid_from": _fmt(v["valid_from"]),
             "valid_to": _fmt(v["valid_to"]) if v["valid_to"] else "current"}
            for v in found.versions
        ]

        self.considered = [
            {"other": (c["right_source_identity"]
                       if c["left_source_identity"] in _keys(found)
                       else c["left_source_identity"]),
             "score": f"{float(c['score']):.3f}",
             "zone": _fmt(c["zone"]),
             "decision": _fmt(c["final_decision"]),
             "decided_by": _fmt(c["decided_by"]),
             "model": _fmt(c["model_name"]),
             "ai_score": (f"{float(c['ai_score']):.3f}"
                          if c["ai_score"] is not None else "—")}
            for c in found.considered
        ]

        self.stat_policies = str(len(found.policies))
        cover = found.total_sum_assured
        self.stat_cover = f"{cover:,.0f}" if cover else "—"
        self.stat_household = str(len(found.household_members))
        self.stat_sources = str(len(found.sources))
        self.stat_contested = str(len(found.lineage))


def _keys(found) -> set[str]:
    """The source keys this party owns, to work out which side of a pair it is."""
    return {str(s["source_party_key"]) for s in found.sources}


_GROUP_LABEL = {
    "owns": "Policies owned",
    "insured": "Cover on this party",
    "services": "Agent book",
    "benefits": "Named as beneficiary",
    "pays": "Pays the premium",
    "other": "Other roles",
}

_GROUP_HINT = {
    "owns": "Contracts this party holds. The owner is who the insurer deals with.",
    "insured": "Policies whose benefit is payable on this party — which is not "
               "the same set as the policies they own.",
    "services": "Policies this party sells or services. A book, not a "
                "relationship: an agent is not exposed to the sums assured.",
    "benefits": "Policies naming this party as a beneficiary.",
    "pays": "Policies this party pays for, whoever owns them.",
    "other": "Roles that do not fall into the groups above.",
}
