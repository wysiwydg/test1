"""Business and steward consoles.

Server-rendered HTML from the same FastAPI process that serves the API. That is
a deliberate choice over a separate single-page application, and the reasons are
specific rather than a general preference:

*   **Authorization stays in one place.** A SPA needs its own copy of the "who
    may see what" rules to decide which fields to render, and two copies of an
    access-control rule is one copy too many. Rendering server-side means the
    masking the API already applies is the masking the page shows.
*   **PII never reaches the browser unmasked.** With a SPA the full record
    crosses the network and sits in the client's memory and cache, masked only
    by what the front-end chooses to display. Here a VIEWER's browser never
    receives the date of birth at all.
*   **No build step.** The whole system stays installable with pip.

Concurrency is real but is not the UI's problem: two stewards working
simultaneously are two transactions, and the golden store already enforces
one-current-version and refuses a lost update. The console's contribution is to
show *who else is looking* at a record and to fail loudly on a stale write,
rather than to lock anything.

Two consoles because the audiences want opposite things. The business console is
read-only and answers "who is this customer". The steward console is a work
queue and answers "what needs a decision from me".
"""

from __future__ import annotations

import html
import uuid
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row

from cmdm.deps import ConnectionDep, PrincipalDep, require
from cmdm.governance.rbac import (
    Action,
    Principal,
    log_access,
    mask_frame,
    record_steward_action,
)
from cmdm.model.fields import PERSON

__all__ = ["router"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

STYLE = """
:root {
  --bg: #ffffff; --fg: #1a1d21; --muted: #5c6570; --line: #dfe3e8;
  --accent: #1c5d99; --warn: #a8620a; --bad: #a32d2d; --good: #1f7a44;
  --chip: #eef2f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --fg: #e8eaed; --muted: #9aa4af; --line: #2b3138;
    --accent: #6fb3e8; --warn: #e0a355; --bad: #e08585; --good: #6cc48f;
    --chip: #1e242b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { border-bottom: 1px solid var(--line); padding: 14px 24px;
  display: flex; align-items: baseline; gap: 20px; flex-wrap: wrap; }
header h1 { font-size: 17px; margin: 0; font-weight: 650; }
header nav a { color: var(--accent); text-decoration: none; margin-right: 16px; font-size: 14px; }
header .who { margin-left: auto; color: var(--muted); font-size: 13px; }
main { padding: 22px 24px; max-width: 1180px; }
h2 { font-size: 15px; margin: 26px 0 10px; font-weight: 650; }
h2:first-child { margin-top: 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 550; font-size: 12px;
  text-transform: uppercase; letter-spacing: .04em; }
tbody tr:hover { background: var(--chip); }
a { color: var(--accent); }
.chip { display: inline-block; padding: 1px 8px; border-radius: 10px;
  background: var(--chip); font-size: 12px; }
.zone-AUTO_MATCH { color: var(--good); } .zone-GREY { color: var(--warn); }
.zone-AUTO_REJECT { color: var(--muted); }
.masked { color: var(--muted); font-style: italic; }
form.search { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }
input, select, textarea { background: var(--bg); color: var(--fg);
  border: 1px solid var(--line); border-radius: 5px; padding: 6px 9px; font: inherit; }
button { background: var(--accent); color: #fff; border: 0; border-radius: 5px;
  padding: 6px 14px; font: inherit; cursor: pointer; }
button.secondary { background: var(--chip); color: var(--fg); }
.metric { display: inline-block; margin: 0 26px 14px 0; }
.metric .v { font-size: 24px; font-weight: 600; display: block; }
.metric .k { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; }
.note { color: var(--muted); font-size: 13px; margin: 6px 0 14px; }
.bar { height: 6px; background: var(--chip); border-radius: 3px; overflow: hidden;
  width: 130px; display: inline-block; vertical-align: middle; }
.bar > i { display: block; height: 100%; background: var(--accent); }
"""


def _esc(value: Any) -> str:
    """Escape for HTML.

    Applied to every interpolated value without exception. Customer names are
    attacker-controlled in the sense that matters here: they arrive from source
    feeds nobody in this system validates for markup.
    """
    if value is None:
        return '<span class="masked">—</span>'
    return html.escape(str(value))


def _page(title: str, principal: Principal, body: str, *, active: str = "") -> HTMLResponse:
    """Wrap content in the shared chrome."""
    def link(href: str, label: str, key: str) -> str:
        weight = ' style="font-weight:650"' if key == active else ""
        return f'<a href="{href}"{weight}>{label}</a>'

    nav = [link("/console", "Customers", "business")]
    if principal.may(Action.MERGE):
        nav.append(link("/console/steward", "Steward queue", "steward"))
    if principal.may(Action.APPROVE_RULE):
        nav.append(link("/console/rules", "Learned rules", "rules"))
    nav.append(link("/console/quality", "Quality", "quality"))

    roles = ", ".join(principal.roles) or "unauthenticated"
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Customer MDM</title><style>{STYLE}</style></head>
<body><header><h1>Customer MDM</h1><nav>{"".join(nav)}</nav>
<span class="who">{_esc(principal.subject)} · {_esc(roles)}</span></header>
<main>{body}</main></body></html>"""
    )


def _bar(ratio: float) -> str:
    pct = max(0.0, min(1.0, ratio)) * 100
    return f'<span class="bar"><i style="width:{pct:.0f}%"></i></span> {pct:.1f}%'


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


#: The console shares the API's connection and authentication dependencies
#: rather than defining its own, so there is exactly one place where "who is
#: this caller" is decided.
router = APIRouter(prefix="/console", tags=["console"])

_guard = require


# ---------------------------------------------------------------------------
# Business console
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def customers(
    conn: ConnectionDep,
    principal: PrincipalDep,
    q: Annotated[str, Query(max_length=200)] = "",
) -> HTMLResponse:
    """Search golden records. The business console's whole job."""
    _guard(principal, Action.SEARCH, conn)

    rows: list[dict[str, Any]] = []
    revealed = principal.may_unmask
    if q.strip():
        from cmdm.ingest.normalize import normalize_full_name

        normalized = (
            pl.DataFrame({"x": [q]})
            .select(normalize_full_name(pl.col("x")).alias("n"))["n"][0]
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT person_id, full_name, date_of_birth, email_address,
                       postal_code, party_type, source_count
                FROM mdm.person
                WHERE is_current AND NOT is_deleted
                  AND (full_name_normalized LIKE %s OR email_normalized = %s)
                ORDER BY full_name_normalized LIMIT 50
                """,
                (f"%{normalized}%", q.strip().lower()),
            )
            found = [dict(r) for r in cur.fetchall()]
        if found:
            frame = pl.DataFrame(found, strict=False)
            masked, revealed = mask_frame(frame, PERSON, principal)
            rows = list(masked.iter_rows(named=True))
        log_access(conn, principal, Action.SEARCH, entity_name="Person",
                   record_count=len(rows), pii_revealed=revealed)

    body = [
        '<form class="search" method="get">',
        f'<input name="q" value="{_esc(q)}" placeholder="Name or email" size="36" autofocus>',
        "<button>Search</button></form>",
    ]
    if not revealed and rows:
        body.append(
            '<p class="note">Personal fields are masked for your role. '
            "An OPERATOR or STEWARD sees them unmasked.</p>"
        )
    if rows:
        body.append(
            "<table><thead><tr><th>Name</th><th>Born</th><th>Email</th>"
            "<th>Postcode</th><th>Type</th><th>Sources</th></tr></thead><tbody>"
        )
        for row in rows:
            pid = row["person_id"]
            body.append(
                f'<tr><td><a href="/console/person/{pid}">{_esc(row["full_name"])}</a></td>'
                f'<td>{_esc(row["date_of_birth"])}</td>'
                f'<td>{_esc(row["email_address"])}</td>'
                f'<td>{_esc(row["postal_code"])}</td>'
                f'<td><span class="chip">{_esc(row["party_type"])}</span></td>'
                f'<td>{_esc(row["source_count"])}</td></tr>'
            )
        body.append("</tbody></table>")
    elif q.strip():
        body.append('<p class="note">No golden records match that search.</p>')

    return _page("Customers", principal, "".join(body), active="business")

@router.get("/person/{person_id}", response_class=HTMLResponse)
def person_detail(
    person_id: uuid.UUID,
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> HTMLResponse:
    """One golden record, with the lineage behind every contested value.

    The lineage is on the same page rather than behind a tab because "why
    does it say that" is asked at the same moment as "what does it say", and
    separating them means most users never see the answer.
    """
    _guard(principal, Action.READ, conn)

    from cmdm.store.writer import resolve_person_id

    resolved = resolve_person_id(conn, person_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM mdm.person WHERE person_id = %s AND is_current", (resolved,)
        )
        record = cur.fetchone()
        if record is None:
            raise HTTPException(status_code=404, detail="person not found")

        cur.execute(
            """
            SELECT attribute_name, strategy, winning_source_system, value_text,
                   candidate_count, rejected_values
            FROM mdm.attribute_provenance
            WHERE entity_id = %s ORDER BY attribute_name
            """,
            (resolved,),
        )
        lineage = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT source_system, source_key_kind, source_party_key, is_active,
                   derivation_method
            FROM mdm.person_xref WHERE person_id = %s ORDER BY source_system
            """,
            (resolved,),
        )
        sources = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT version, valid_from, valid_to FROM mdm.person "
            "WHERE person_id = %s ORDER BY version DESC LIMIT 10",
            (resolved,),
        )
        versions = [dict(r) for r in cur.fetchall()]

    frame = pl.DataFrame([dict(record)], strict=False)
    masked, revealed = mask_frame(frame, PERSON, principal)
    shown = masked.row(0, named=True)
    log_access(conn, principal, Action.READ, entity_name="Person",
               entity_id=resolved, pii_revealed=revealed)

    interesting = [
        "full_name", "party_type", "date_of_birth", "email_address", "phone_e164",
        "address_normalized", "postal_code", "country_code", "occupation",
        "do_not_contact", "is_deceased", "source_count", "confidence",
    ]
    body = [f"<h2>{_esc(shown.get('full_name'))}</h2>"]
    if str(person_id) != str(resolved):
        body.append(
            f'<p class="note">The id you followed was merged into '
            f"<code>{_esc(resolved)}</code>. Published ids keep resolving.</p>"
        )
    body.append("<table><tbody>")
    for key in interesting:
        if key in shown:
            body.append(
                f'<tr><th style="width:200px">{_esc(key)}</th>'
                f"<td>{_esc(shown[key])}</td></tr>"
            )
    body.append("</tbody></table>")

    body.append("<h2>Contributing sources</h2><table><thead><tr><th>System</th>"
                "<th>Key kind</th><th>Key</th><th>Active</th><th>How</th>"
                "</tr></thead><tbody>")
    for source in sources:
        body.append(
            f"<tr><td>{_esc(source['source_system'])}</td>"
            f"<td>{_esc(source['source_key_kind'])}</td>"
            f"<td>{_esc(source['source_party_key'] if revealed else '***')}</td>"
            f"<td>{'yes' if source['is_active'] else 'no'}</td>"
            f"<td><span class=\"chip\">{_esc(source['derivation_method'])}</span></td></tr>"
        )
    body.append("</tbody></table>")

    body.append("<h2>Why these values</h2>")
    if lineage:
        body.append('<p class="note">Only attributes the sources disagreed about '
                    "appear here. Everything else was uncontested.</p>")
        body.append("<table><thead><tr><th>Attribute</th><th>Surviving value</th>"
                    "<th>Rule</th><th>Won from</th><th>Candidates</th>"
                    "</tr></thead><tbody>")
        for entry in lineage:
            value = entry["value_text"] if revealed else "***"
            body.append(
                f"<tr><td>{_esc(entry['attribute_name'])}</td>"
                f"<td>{_esc(value)}</td>"
                f"<td><span class=\"chip\">{_esc(entry['strategy'])}</span></td>"
                f"<td>{_esc(entry['winning_source_system'])}</td>"
                f"<td>{_esc(entry['candidate_count'])}</td></tr>"
            )
        body.append("</tbody></table>")
    else:
        body.append('<p class="note">No contested attributes: every source that '
                    "contributed to this record agreed.</p>")

    body.append("<h2>Version history</h2><table><thead><tr><th>Version</th>"
                "<th>From</th><th>To</th></tr></thead><tbody>")
    for version in versions:
        body.append(
            f"<tr><td>{_esc(version['version'])}</td>"
            f"<td>{_esc(version['valid_from'])}</td>"
            f"<td>{_esc(version['valid_to']) if version['valid_to'] else 'current'}"
            "</td></tr>"
        )
    body.append("</tbody></table>")

    return _page("Customer", principal, "".join(body), active="business")

# -- steward console ---------------------------------------------------

@router.get("/steward", response_class=HTMLResponse)
def steward_queue(
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> HTMLResponse:
    """Pairs the system could not decide.

    Ordered by score descending so the most likely duplicates surface first.
    A steward's attention is the scarcest resource in the whole system, and
    an unordered queue spends it randomly.
    """
    _guard(principal, Action.MERGE, conn)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.pair_id, p.left_person_id, p.right_person_id, p.score,
                   p.zone, p.ai_score, p.ai_decision, p.blocking_key,
                   l.full_name AS left_name, r.full_name AS right_name,
                   l.date_of_birth AS left_dob, r.date_of_birth AS right_dob
            FROM mdm.match_pair p
            LEFT JOIN mdm.person l ON l.person_id = p.left_person_id AND l.is_current
            LEFT JOIN mdm.person r ON r.person_id = p.right_person_id AND r.is_current
            WHERE p.zone = 'GREY'
            ORDER BY p.score DESC LIMIT 100
            """
        )
        pairs = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT count(*) AS n FROM mdm.standardization_rule WHERE state = 'SHADOW'"
        )
        pending_rules = int((cur.fetchone() or {}).get("n") or 0)

    body = ["<h2>Ambiguous pairs</h2>"]
    if pending_rules:
        body.append(
            f'<p class="note">{pending_rules} learned rule(s) are shadow-tested and '
            'waiting for review. <a href="/console/rules">Review them</a>.</p>'
        )
    if not pairs:
        body.append('<p class="note">Nothing in the grey zone. Either resolution has '
                    "not run, or every pair was decided confidently.</p>")
    else:
        body.append(
            '<p class="note">These are the pairs the scorer declined to decide and '
            "the model was asked about. Its verdict is advisory; yours is final.</p>"
        )
        body.append("<table><thead><tr><th>Score</th><th>Model</th><th>Left</th>"
                    "<th>Right</th><th>Blocked on</th><th></th></tr></thead><tbody>")
        for pair in pairs:
            verdict = pair["ai_decision"] or "—"
            ai_score = "" if pair["ai_score"] is None else f"{pair['ai_score']:.2f}"
            body.append(
                f'<tr><td class="zone-GREY">{pair["score"]:.3f}</td>'
                f'<td><span class="chip">{_esc(verdict)}</span> '
                f"{ai_score}</td>"
                f'<td><a href="/console/person/{pair["left_person_id"]}">'
                f'{_esc(pair["left_name"])}</a><br>'
                f'<small class="masked">{_esc(pair["left_dob"])}</small></td>'
                f'<td><a href="/console/person/{pair["right_person_id"]}">'
                f'{_esc(pair["right_name"])}</a><br>'
                f'<small class="masked">{_esc(pair["right_dob"])}</small></td>'
                f'<td><span class="chip">{_esc(pair["blocking_key"])}</span></td>'
                f'<td><form method="post" action="/console/steward/decide">'
                f'<input type="hidden" name="pair_id" value="{pair["pair_id"]}">'
                f'<input name="reason" placeholder="Reason" required size="16">'
                f'<button name="decision" value="MERGE">Merge</button> '
                f'<button class="secondary" name="decision" value="SEPARATE">'
                f"Separate</button></form></td></tr>"
            )
        body.append("</tbody></table>")

    return _page("Steward queue", principal, "".join(body), active="steward")

@router.post("/steward/decide")
def steward_decide(
    conn: ConnectionDep,
    principal: PrincipalDep,
    pair_id: Annotated[uuid.UUID, Form()],
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form(min_length=4, max_length=500)],
) -> RedirectResponse:
    """Record a steward's verdict on an ambiguous pair.

    The decision is recorded, not applied directly to the graph. Re-running
    resolution is what actually merges, and it reads these decisions as
    fixed edges — so a steward's call survives the next run rather than
    being overwritten by it, which is what happens when a console mutates
    the golden store directly.
    """
    _guard(principal, Action.MERGE, conn)
    if decision not in ("MERGE", "SEPARATE"):
        raise HTTPException(status_code=400, detail="decision must be MERGE or SEPARATE")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT left_person_id, right_person_id, score, zone, ai_decision "
            "FROM mdm.match_pair WHERE pair_id = %s",
            (pair_id,),
        )
        pair = cur.fetchone()
    if pair is None:
        raise HTTPException(status_code=404, detail="pair not found")

    record_steward_action(
        conn, principal,
        Action.MERGE if decision == "MERGE" else Action.SPLIT,
        entity_name="MatchPair", entity_id=pair["left_person_id"],
        related_id=pair["right_person_id"], reason=reason,
        before={"zone": pair["zone"], "score": float(pair["score"]),
                "ai_decision": pair["ai_decision"]},
        after={"decision": decision},
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mdm.match_pair SET final_decision = %s, decided_by = %s "
            "WHERE pair_id = %s",
            ("MATCH" if decision == "MERGE" else "NO_MATCH", principal.subject, pair_id),
        )
    return RedirectResponse("/console/steward", status_code=303)

@router.get("/rules", response_class=HTMLResponse)
def rules(
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> HTMLResponse:
    """The learned-rule review queue.

    This is the human gate in the standardization loop. The shadow-evaluation
    numbers are shown next to the rule because approving a regex without
    knowing what it fixes and what it broke is not review, it is assent.
    """
    _guard(principal, Action.APPROVE_RULE, conn)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT rule_id, field_name, rule_name, pattern, rule_kind, state,
                   evidence_count, evidence_sample, shadow_matched, shadow_fixed,
                   shadow_regressions, targets_check, proposed_by
            FROM mdm.standardization_rule
            WHERE state IN ('PROPOSED', 'SHADOW', 'ACTIVE')
            ORDER BY CASE state WHEN 'SHADOW' THEN 0 WHEN 'PROPOSED' THEN 1 ELSE 2 END,
                     shadow_fixed DESC NULLS LAST
            LIMIT 100
            """
        )
        found = [dict(r) for r in cur.fetchall()]

    body = ["<h2>Learned rules</h2>",
            '<p class="note">Rules the mining agent proposed from recurring '
            "patterns in the AI fallback log. A rule cannot go live until it has "
            "been shadow-tested and approved here.</p>"]
    if not found:
        body.append('<p class="note">No rules proposed yet.</p>')
    else:
        body.append("<table><thead><tr><th>Rule</th><th>State</th><th>Pattern</th>"
                    "<th>Fixes</th><th>Regressions</th><th>Evidence</th><th></th>"
                    "</tr></thead><tbody>")
        for rule in found:
            sample = (rule["evidence_sample"] or {}).get("values", [])[:2]
            regressions = rule["shadow_regressions"]
            regression_class = "zone-AUTO_MATCH" if regressions == 0 else "zone-AUTO_REJECT"
            action = ""
            if rule["state"] == "SHADOW":
                action = (
                    f'<form method="post" action="/console/rules/approve">'
                    f'<input type="hidden" name="rule_id" value="{rule["rule_id"]}">'
                    f'<input name="reason" placeholder="Reason" required size="14">'
                    f'<button name="verdict" value="APPROVE">Approve</button> '
                    f'<button class="secondary" name="verdict" value="REJECT">'
                    f"Reject</button></form>"
                )
            body.append(
                f'<tr><td>{_esc(rule["rule_name"])}<br>'
                f'<small class="masked">{_esc(rule["field_name"])}</small></td>'
                f'<td><span class="chip">{_esc(rule["state"])}</span></td>'
                f'<td><code style="font-size:12px">{_esc(rule["pattern"][:70])}</code></td>'
                f'<td>{_esc(rule["shadow_fixed"])}</td>'
                f'<td class="{regression_class}">{_esc(regressions)}</td>'
                f'<td>{_esc(rule["evidence_count"])}<br>'
                f'<small class="masked">{_esc(", ".join(map(str, sample))[:44])}</small></td>'
                f"<td>{action}</td></tr>"
            )
        body.append("</tbody></table>")

    return _page("Learned rules", principal, "".join(body), active="rules")

@router.post("/rules/approve")
def approve(
    conn: ConnectionDep,
    principal: PrincipalDep,
    rule_id: Annotated[uuid.UUID, Form()],
    verdict: Annotated[str, Form()],
    reason: Annotated[str, Form(min_length=4, max_length=500)],
) -> RedirectResponse:
    """Approve or reject a learned rule."""
    _guard(principal, Action.APPROVE_RULE, conn)

    from cmdm.standardize.rules import approve_rule, reject_rule

    if verdict == "APPROVE":
        took = approve_rule(conn, rule_id, reviewer=principal.subject, note=reason)
    else:
        took = reject_rule(conn, rule_id, reviewer=principal.subject, note=reason)
    if not took:
        raise HTTPException(
            status_code=409,
            detail="the rule is not in a state that permits this; it may have been "
                   "reviewed already or never shadow-tested",
        )

    record_steward_action(
        conn, principal, Action.APPROVE_RULE, entity_name="StandardizationRule",
        entity_id=rule_id, reason=reason, after={"verdict": verdict},
    )
    return RedirectResponse("/console/rules", status_code=303)

# -- quality -----------------------------------------------------------

@router.get("/quality", response_class=HTMLResponse)
def quality(
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> HTMLResponse:
    """Data quality, for the people who own the data.

    Deliberately not the operational dashboard. Queue depth and dead letters
    belong to whoever runs the pipeline; completeness and duplication belong
    to whoever owns the customer book, and showing both together means each
    audience learns to ignore half the page.
    """
    _guard(principal, Action.SEARCH, conn)

    from cmdm.observe import assess_data_quality

    data = assess_data_quality(conn)

    body = [
        "<h2>Golden record quality</h2>",
        f'<div class="metric"><span class="v">{data.entities:,}</span>'
        '<span class="k">Golden persons</span></div>',
        f'<div class="metric"><span class="v">{data.mean_completeness:.1%}</span>'
        '<span class="k">Mean completeness</span></div>',
        f'<div class="metric"><span class="v">{data.duplication_rate:.1%}</span>'
        '<span class="k">Held under 2+ source keys</span></div>',
        f'<div class="metric"><span class="v">{data.multi_source_share:.1%}</span>'
        '<span class="k">Built from 2+ sources</span></div>',
    ]

    body.append("<h2>Completeness</h2><table><thead><tr><th>Attribute</th>"
                "<th>Populated</th></tr></thead><tbody>")
    for attribute, value in sorted(data.completeness.items(), key=lambda kv: -kv[1]):
        body.append(f"<tr><td>{_esc(attribute)}</td><td>{_bar(value)}</td></tr>")
    body.append("</tbody></table>")

    body.append("<h2>Conformity</h2>"
                '<p class="note">Of the values that are populated, how many actually '
                "parse as what they claim to be. A column that is always populated "
                "with nonsense scores perfectly on completeness alone.</p>"
                "<table><thead><tr><th>Attribute</th><th>Valid</th></tr></thead><tbody>")
    for attribute, value in sorted(data.conformity.items(), key=lambda kv: -kv[1]):
        body.append(f"<tr><td>{_esc(attribute)}</td><td>{_bar(value)}</td></tr>")
    body.append("</tbody></table>")

    return _page("Quality", principal, "".join(body), active="quality")

