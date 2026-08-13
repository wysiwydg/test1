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
import io
import uuid
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
from fastapi import File as FileParam
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from psycopg.rows import dict_row

from cmdm.deps import SESSION_COOKIE, ConnectionDep, PrincipalDep, require
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
/* A uuid is 36 characters and wraps to four lines in a narrow column, which
   triples the height of every row in the browser. Monospace and no-wrap keeps
   the whole id on one line and still selectable, which truncating would not. */
.id { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px;
  white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
/* Ten columns of an entity are wider than the column of text the rest of the
   console is set in. The table scrolls inside this rather than pushing the
   page sideways, which would move the navigation off screen with it. */
.wide { overflow-x: auto; }
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

    nav = []
    if principal.may(Action.SEARCH):
        nav.append(link("/console", "Customers", "business"))
        nav.append(link("/console/entities", "Entities", "entities"))
    if principal.may(Action.EXPORT):
        nav.append(link("/console/export", "Export", "export"))
    if principal.may(Action.SUBMIT):
        nav.append(link("/console/ingest", "Ingestion", "ingest"))
    if principal.may(Action.MERGE):
        nav.append(link("/console/steward", "Steward queue", "steward"))
    if principal.may(Action.APPROVE_RULE):
        nav.append(link("/console/rules", "Learned rules", "rules"))
    if principal.may(Action.SEARCH):
        nav.append(link("/console/quality", "Quality", "quality"))

    if principal.roles:
        who = (
            f"{_esc(principal.subject)} · {_esc(', '.join(principal.roles))} "
            '<form method="post" action="/console/logout" style="display:inline">'
            '<button class="secondary" style="padding:2px 8px;font-size:12px">'
            "Sign out</button></form>"
        )
    else:
        who = '<a href="/console/login">Sign in</a>'

    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Customer MDM</title><style>{STYLE}</style></head>
<body><header><h1>Customer MDM</h1><nav>{"".join(nav)}</nav>
<span class="who">{who}</span></header>
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


class LoginRequired(Exception):
    """Raised when an unauthenticated browser reaches a console page.

    A 403 with a JSON body is the right answer for an API client and the wrong
    one for a person: they have no way to supply a key from it. The application
    turns this into a redirect to the sign-in page instead. Anyone who *is*
    signed in and simply lacks the permission still gets the 403 — being told to
    sign in again when you are already signed in teaches people to distrust the
    message.
    """


def _guard(principal: Principal, action: str, conn: Any) -> None:
    """Authorize a console request, or send the caller to sign in."""
    if not principal.roles:
        raise LoginRequired
    require(principal, action, conn)


@router.get("/login", response_class=HTMLResponse)
def login_form(
    principal: PrincipalDep,
    next: Annotated[str, Query(max_length=200)] = "/console",
) -> HTMLResponse:
    """Exchange an API key for a session cookie."""
    target = next if next.startswith("/console") else "/console"
    body = (
        "<h2>Sign in</h2>"
        '<p class="note">Paste the API key issued for your role. '
        "<code>python -m scripts.bootstrap</code> prints one per console "
        "audience, once.</p>"
        '<form class="search" method="post" action="/console/login">'
        f'<input type="hidden" name="next" value="{_esc(target)}">'
        '<input type="password" name="key" placeholder="API key" size="44" '
        "required autofocus><button>Sign in</button></form>"
    )
    if principal.roles:
        body += (
            f'<p class="note">You are already signed in as '
            f"{_esc(principal.subject)}.</p>"
        )
    return _page("Sign in", principal, body)


@router.post("/login")
def login(
    conn: ConnectionDep,
    key: Annotated[str, Form(max_length=500)],
    next: Annotated[str, Form(max_length=200)] = "/console",
) -> RedirectResponse:
    """Validate the key before storing it.

    Checked here rather than left for the next request so a mistyped key fails
    at the sign-in page, where the person can see it, instead of turning every
    subsequent page into an unexplained redirect back here.

    Open redirects are the standard bug in this shape of handler, so ``next`` is
    required to be a console path rather than merely a relative one.
    """
    from cmdm.governance.rbac import authenticate

    principal = authenticate(conn, key)
    if not principal.roles:
        return RedirectResponse("/console/login?next=/console", status_code=303)

    log_access(conn, principal, Action.READ, entity_name="ConsoleSession")
    response = RedirectResponse(
        next if next.startswith("/console") else "/console", status_code=303
    )
    response.set_cookie(
        SESSION_COOKIE, key,
        httponly=True, samesite="lax", max_age=12 * 3600, path="/",
    )
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    """Drop the session cookie."""
    response = RedirectResponse("/console/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


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

# ---------------------------------------------------------------------------
# Entity dashboard
# ---------------------------------------------------------------------------


@router.get("/entities", response_class=HTMLResponse)
def entities(
    conn: ConnectionDep,
    principal: PrincipalDep,
    show: Annotated[str, Query(max_length=20)] = "",
    page: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> HTMLResponse:
    """What is actually in the three entities, and a way into each.

    The canonical model has three entities and until now the console showed
    one of them. A business owner asking "what is in there" was being answered
    about Person only, which is the entity the matching happens to be about
    rather than the one the data arrives as.
    """
    _guard(principal, Action.SEARCH, conn)

    from cmdm.export import PAGE_SIZE, browse_entity, entity_names, entity_overview

    totals = entity_overview(conn)
    log_access(conn, principal, Action.SEARCH, entity_name="EntityDashboard",
               record_count=int(totals.get("persons") or 0))

    body = [
        "<h2>The canonical model</h2>",
        '<p class="note">Three entities. Policy is the grain the data arrives '
        "in; Person is what resolution collapses to; Relationship is the "
        "role-bearing edge between them.</p>",
        _metric(totals.get("policies"), "Policies"),
        _metric(totals.get("persons"), "Golden persons"),
        _metric(totals.get("relationships"), "Relationships"),
        _metric(totals.get("landed_rows"), "Landed source rows"),
    ]

    body.append("<h2>Person</h2>")
    body.append(_metric(totals.get("natural_persons"), "Natural persons"))
    body.append(_metric(totals.get("legal_entities"), "Trusts, estates, companies"))
    body.append(_metric(totals.get("multi_source_persons"), "Built from 2+ sources"))
    body.append(_metric(totals.get("revised_persons"), "Revised since first write"))
    body.append(_metric(totals.get("person_keys"), "Source keys crosswalked"))

    body.append("<h2>Policy and Relationship</h2>")
    body.append(_metric(totals.get("policy_sources"), "Source systems"))
    body.append(_metric(totals.get("policy_keys"), "Policy keys crosswalked"))
    body.append(_breakdown("Roles on the edge", totals.get("by_role", []), "role"))
    body.append(
        _breakdown("Policy status", totals.get("by_status", []), "policy_status")
    )
    body.append(_breakdown("Party type", totals.get("by_party_type", []), "party_type"))

    names = entity_names()
    # Policy by default: it is the grain the data arrives in, and a dashboard
    # that opens on counts alone answers "how much" when the question asked was
    # "what is in there".
    show = show or names[0]

    body.append("<h2>Browse</h2><p>")
    for name in names:
        weight = ' style="font-weight:650"' if name == show else ""
        body.append(f'<a href="/console/entities?show={name}"{weight}>'
                    f"{name.title()}</a> &nbsp; ")
    if principal.may(Action.EXPORT):
        body.append(f'&nbsp;·&nbsp; <a href="/console/export/entity/{show}.csv">'
                    f"Download all {_esc(show)} records</a>")
    body.append("</p>")

    if show in names:
        rows, total = browse_entity(conn, show, principal=principal, page=page)
        body.append(
            f'<p class="note">{total:,} current {show} rows. '
            f"Showing {page * PAGE_SIZE + 1:,}–"
            f"{min((page + 1) * PAGE_SIZE, total):,}.</p>"
        )
        if rows and not principal.may_unmask:
            body.append('<p class="note">Personal fields are masked for your '
                        "role. An OPERATOR or STEWARD sees them unmasked.</p>")
        if rows:
            columns = list(rows[0])
            body.append('<div class="wide"><table><thead><tr>')
            body.extend(f"<th>{_esc(c)}</th>" for c in columns)
            body.append("</tr></thead><tbody>")
            for row in rows:
                body.append("<tr>")
                for column in columns:
                    body.append(_browse_cell(column, row[column]))
                body.append("</tr>")
            body.append("</tbody></table></div>")

            links = []
            if page:
                links.append(f'<a href="/console/entities?show={show}&page={page - 1}">'
                             "&larr; previous</a>")
            if (page + 1) * PAGE_SIZE < total:
                links.append(f'<a href="/console/entities?show={show}&page={page + 1}">'
                             "next &rarr;</a>")
            if links:
                body.append(f'<p>{" &nbsp; ".join(links)}</p>')

    return _page("Entities", principal, "".join(body), active="entities")


def _browse_cell(column: str, value: Any) -> str:
    """Render one browser cell, typed by what the column is.

    Amounts arrive from the database as Decimal and stringify as
    ``80000.0000``, which is four digits of false precision on a figure a
    business reader is scanning down a column. They are shown grouped and to
    two places, right-aligned so the magnitudes line up.
    """
    if value is None:
        return '<td class="masked">—</td>'

    if column == "person_id":
        return (f'<td><a class="id" href="/console/person/{_esc(value)}">'
                f"{_esc(value)}</a></td>")
    if column.endswith("_id"):
        return f'<td><span class="id">{_esc(value)}</span></td>'
    if column.endswith("_amount") or column.endswith("_percent"):
        try:
            return f'<td class="num">{float(value):,.2f}</td>'
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    if isinstance(value, int) and not isinstance(value, bool):
        return f'<td class="num">{value:,}</td>'
    return f"<td>{_esc(value)}</td>"


def _metric(value: Any, label: str) -> str:
    shown = f"{value:,}" if isinstance(value, int) else _esc(value)
    return (f'<div class="metric"><span class="v">{shown}</span>'
            f'<span class="k">{_esc(label)}</span></div>')


def _breakdown(title: str, rows: list[dict[str, Any]], key: str) -> str:
    """A small counted breakdown, or nothing when there is nothing to show."""
    if not rows:
        return ""
    cells = " ".join(
        f'<span class="chip">{_esc(r[key])} {int(r["n"]):,}</span>' for r in rows
    )
    return f'<p class="note">{_esc(title)}: {cells}</p>'


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.get("/export", response_class=HTMLResponse)
def export_page(
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> HTMLResponse:
    """What can be exported, and what each file is for."""
    _guard(principal, Action.EXPORT, conn)

    from cmdm.export import entity_names, entity_overview, source_shaped_columns

    totals = entity_overview(conn)
    mappings = _available_mappings()

    body = [
        "<h2>Hand the source system its extract back</h2>",
        '<p class="note">One row per row delivered, the columns the mapping '
        "reads, in the grain the file arrived in — plus the MDM ids. This is "
        "the file a source system can actually load: it joins to what it "
        "already has, because it <em>is</em> what it already has.</p>",
    ]

    if not mappings:
        body.append('<p class="note">No mappings installed.</p>')
    for name in mappings:
        try:
            from cmdm.ingest.mapping import load_mapping
            from cmdm.worker import MAPPINGS_DIR

            mapping = load_mapping(MAPPINGS_DIR / f"{name}.toml")
            id_columns = ", ".join(source_shaped_columns(mapping))
        except Exception:  # pragma: no cover - a broken mapping file
            id_columns = "?"
        body.append(
            f"<h2>{_esc(name)}</h2>"
            f'<p class="note">Appends <code>{_esc(id_columns)}</code> to every '
            f'row. {int(totals.get("landed_rows") or 0):,} rows landed.</p>'
            f'<p><a href="/console/export/source/{_esc(name)}.csv">'
            "Download as delivered</a> &nbsp;·&nbsp; "
            f'<a href="/console/export/source/{_esc(name)}.csv?values=golden">'
            "Download with golden values</a></p>"
            '<p class="note"><strong>As delivered</strong> keeps every value '
            "exactly as it arrived, so the file is recognisable as the one that "
            "was sent. <strong>Golden values</strong> replaces the party "
            "attributes with the ones that survived resolution, for a system "
            "adopting the cleaned data rather than only the keys.</p>"
        )

    body.append("<h2>Entities</h2>")
    body.append('<p class="note">The golden records themselves, one file per '
                "entity, every column the registry declares.</p><p>")
    labels = {"person": "persons", "policy": "policies",
              "relationship": "relationships"}
    body.append(" &nbsp;·&nbsp; ".join(
        f'<a href="/console/export/entity/{name}.csv">{name.title()}</a> '
        f'<span class="masked">({int(totals.get(labels[name]) or 0):,} rows)</span>'
        for name in entity_names()
    ))
    body.append("</p>")

    if not principal.may_unmask:
        body.append('<p class="note">Personal fields will be masked in these '
                    "files for your role.</p>")

    return _page("Export", principal, "".join(body), active="export")


@router.get("/export/entity/{entity}.csv")
def export_entity_csv(
    entity: str,
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    """Stream one entity's golden records."""
    _guard(principal, Action.EXPORT, conn)

    from cmdm.export import ENTITY_SPECS, export_entity

    if entity not in ENTITY_SPECS:
        raise HTTPException(status_code=404, detail=f"unknown entity {entity!r}")

    log_access(conn, principal, Action.EXPORT, entity_name=ENTITY_SPECS[entity].name,
               pii_revealed=principal.may_unmask, detail={"format": "csv"})

    return StreamingResponse(
        export_entity(conn, entity, principal=principal),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{entity}.csv"'},
    )


@router.get("/export/source/{mapping_name}.csv")
def export_source_csv(
    mapping_name: str,
    conn: ConnectionDep,
    principal: PrincipalDep,
    values: Annotated[str, Query(pattern="^(as-delivered|golden)$")] = "as-delivered",
) -> StreamingResponse:
    """Stream the delivered extract back with MDM ids attached."""
    _guard(principal, Action.EXPORT, conn)

    from cmdm.export import clear_golden_cache, export_source_shaped
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import MAPPINGS_DIR

    path = (MAPPINGS_DIR / f"{mapping_name}.toml").resolve()
    if path.parent != MAPPINGS_DIR or not path.exists():
        raise HTTPException(status_code=404, detail=f"unknown mapping {mapping_name!r}")

    mapping = load_mapping(path)
    log_access(conn, principal, Action.EXPORT, entity_name="SourceExtract",
               pii_revealed=principal.may_unmask,
               detail={"mapping": mapping_name, "values": values})

    def stream():
        try:
            yield from export_source_shaped(
                conn, mapping, principal=principal, values=values
            )
        finally:
            clear_golden_cache()

    suffix = "" if values == "as-delivered" else "-golden"
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="{mapping_name}-with-mdm-ids{suffix}.csv"'
        },
    )


# ---------------------------------------------------------------------------
# Ingestion console
# ---------------------------------------------------------------------------
#
# The operator's view of the same two steps the API exposes: submit a file, and
# let a worker process it. It exists because the people who own a feed are not
# the people who hold an API key, and "did last night's extract load" is a
# question asked far more often than any other in this system.
#
# The page shows the queue rather than hiding it. A batch is accepted, then
# queued, then processed, and an operator who cannot see that a batch is sitting
# in the queue will conclude the upload failed and send it again.


def _batch_states(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT b.batch_id, b.source_system, b.mapping_name, b.filename,
                   b.origin, b.state, b.row_count, b.accepted_count,
                   b.submitted_by, b.submitted_at, b.completed_at, b.last_error,
                   b.validation_report,
                   q.state AS job_state, q.attempts, q.last_error AS job_error
            FROM mdm.ingest_batch b
            LEFT JOIN mdm.work_queue q
                   ON q.dedupe_key = 'batch:' || b.batch_id::text
            ORDER BY b.submitted_at DESC
            LIMIT 25
            """
        )
        return [dict(r) for r in cur.fetchall()]


def _available_mappings() -> list[str]:
    from cmdm.worker import MAPPINGS_DIR

    return sorted(p.stem for p in MAPPINGS_DIR.glob("*.toml"))


@router.get("/ingest", response_class=HTMLResponse)
def ingest(
    conn: ConnectionDep,
    principal: PrincipalDep,
    message: Annotated[str, Query(max_length=2000)] = "",
) -> HTMLResponse:
    """Submit a file, and see what happened to the ones already submitted."""
    _guard(principal, Action.SUBMIT, conn)

    from cmdm.db.queue import QUEUE_STANDARDIZE, WorkQueue

    depth = WorkQueue(conn).depth(QUEUE_STANDARDIZE)
    pending = depth.get("PENDING", 0) + depth.get("FAILED", 0)
    batches = _batch_states(conn)
    mappings = _available_mappings()

    options = "".join(f"<option>{_esc(m)}</option>" for m in mappings)
    body = []
    if message:
        body.append(f'<p class="note">{_esc(message)}</p>')

    body.append(
        "<h2>Submit a batch</h2>"
        '<p class="note">A CSV in the shape the chosen mapping expects. It is '
        "validated as you submit it — you find out which columns are wrong now, "
        "not tomorrow — then landed and queued. Nothing is written to the "
        "customer book until a worker processes it.</p>"
        '<form class="search" method="post" action="/console/ingest/upload" '
        'enctype="multipart/form-data">'
        f'<select name="mapping_name">{options}</select>'
        '<input type="file" name="file" accept=".csv,text/csv" required>'
        "<button>Submit</button></form>"
    )

    body.append(
        f"<h2>Queue</h2><div class=\"metric\"><span class=\"v\">{pending}</span>"
        '<span class="k">Waiting to be processed</span></div>'
        f'<div class="metric"><span class="v">{depth.get("RUNNING", 0)}</span>'
        '<span class="k">In flight</span></div>'
        f'<div class="metric"><span class="v">{depth.get("DEAD", 0)}</span>'
        '<span class="k">Dead-lettered</span></div>'
    )
    if pending:
        body.append(
            '<form method="post" action="/console/ingest/process">'
            f'<button>Process {pending} queued batch(es) now</button></form>'
            '<p class="note">This runs the pipeline in the request, which is fine '
            "for a file of this size and is how you work without a worker daemon. "
            "In production <code>python -m cmdm.worker serve</code> does this "
            "continuously and this button is a manual nudge.</p>"
        )
    else:
        body.append('<p class="note">Nothing queued.</p>')

    body.append(
        "<h2>Recent batches</h2><table><thead><tr><th>Submitted</th><th>File</th>"
        "<th>Mapping</th><th>Rows</th><th>State</th><th>Job</th><th>Detail</th>"
        "</tr></thead><tbody>"
    )
    if not batches:
        body.append('<tr><td colspan="7" class="masked">No batches yet.</td></tr>')
    for batch in batches:
        report = batch["validation_report"] or {}
        issues = report.get("issues", [])
        detail = batch["last_error"] or batch["job_error"] or "; ".join(
            f"{i['severity']}: {i['message']}" for i in issues[:2]
        )
        state_class = {
            "COMPLETED": "zone-AUTO_MATCH",
            "REJECTED": "zone-AUTO_REJECT",
            "FAILED": "zone-AUTO_REJECT",
        }.get(batch["state"], "zone-GREY")
        attempts = f" ×{batch['attempts']}" if batch["attempts"] else ""
        body.append(
            f'<tr><td>{_esc(str(batch["submitted_at"])[:19])}<br>'
            f'<small class="masked">{_esc(batch["submitted_by"])}</small></td>'
            f'<td>{_esc(batch["filename"])}</td>'
            f'<td>{_esc(batch["mapping_name"])}</td>'
            f'<td>{_esc(batch["row_count"])}</td>'
            f'<td class="{state_class}">{_esc(batch["state"])}</td>'
            f'<td><span class="chip">{_esc(batch["job_state"] or "—")}</span>'
            f'{attempts}</td>'
            f'<td><small>{_esc(detail[:180]) if detail else ""}</small></td></tr>'
        )
    body.append("</tbody></table>")

    return _page("Ingestion", principal, "".join(body), active="ingest")


@router.post("/ingest/upload")
async def ingest_upload(
    conn: ConnectionDep,
    principal: PrincipalDep,
    mapping_name: Annotated[str, Form(max_length=100)],
    file: Annotated[UploadFile, FileParam()],
) -> RedirectResponse:
    """Accept a file from the browser, through the same path as the API.

    Deliberately the same ``accept_batch`` call the API endpoint makes, not a
    console-specific variant. A file uploaded by a person and a file posted by a
    scheduler must be validated identically or the console becomes a way to get
    data in that the API would have refused.
    """
    _guard(principal, Action.SUBMIT, conn)

    from cmdm.ingest.landing import accept_batch
    from cmdm.ingest.mapping import load_mapping
    from cmdm.worker import MAPPINGS_DIR

    path = (MAPPINGS_DIR / f"{mapping_name}.toml").resolve()
    if path.parent != MAPPINGS_DIR or not path.exists():
        raise HTTPException(status_code=404, detail=f"unknown mapping {mapping_name!r}")

    payload = await file.read()
    try:
        raw = pl.read_csv(io.BytesIO(payload), infer_schema_length=0)
    except Exception as exc:
        return _redirect_ingest(f"That file could not be read as CSV: {exc}")

    batch_id, report, enqueued = accept_batch(
        conn, raw, load_mapping(path), origin="CONSOLE",
        filename=file.filename, submitted_by=principal.subject,
    )
    log_access(conn, principal, Action.SUBMIT, entity_name="Batch",
               entity_id=batch_id, record_count=raw.height,
               detail={"accepted": report.accepted, "enqueued": enqueued})

    if enqueued:
        note = (
            f"Accepted {raw.height} rows and queued them. "
            f"{len(report.warnings)} warning(s)."
        )
    elif report.accepted:
        note = "That exact file was already accepted; nothing was queued again."
    else:
        note = "Rejected: " + "; ".join(i.message for i in report.errors)
    return _redirect_ingest(note)


def _redirect_ingest(message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(
        f"/console/ingest?message={quote(message[:1000])}", status_code=303
    )


@router.post("/ingest/process")
def ingest_process(
    conn: ConnectionDep,
    principal: PrincipalDep,
) -> RedirectResponse:
    """Run the queued batches now.

    Uses its own connections rather than the request's: each job is its own
    transaction, and sharing the request transaction across several would make
    one bad batch roll back the good ones processed before it.

    Bounded rather than unbounded. A console click should return to a page, not
    hold a request open for however long the backlog happens to be.
    """
    _guard(principal, Action.SUBMIT, conn)

    from cmdm.db.engine import connect
    from cmdm.worker import drain

    outcomes = drain(connect, limit=5)
    if not outcomes:
        return _redirect_ingest("Nothing was queued.")

    failed = [o for o in outcomes if not o["ok"]]
    persons = sum(o.get("golden_persons", 0) for o in outcomes if o["ok"])
    note = (
        f"Processed {len(outcomes) - len(failed)} batch(es) into {persons:,} "
        f"golden persons."
    )
    if failed:
        note += f" {len(failed)} failed: " + "; ".join(
            str(o["error"])[:200] for o in failed
        )
    return _redirect_ingest(note)


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

    # Scoped to the newest run. Every run re-scores the same pairs, so without
    # this the queue grows by the whole grey zone every night and a steward
    # reviews last week's pairs again alongside today's.
    #
    # Two lists, not one sorted list. A decision made now does not take effect
    # until the batch is processed again, and a steward who cannot see the
    # verdict they just recorded will reasonably conclude the button did
    # nothing — which is how the same pair gets decided four times.
    query = """
        SELECT p.pair_id, p.left_person_id, p.right_person_id, p.score,
               p.zone, p.ai_score, p.ai_decision, p.blocking_key,
               p.final_decision, p.decided_by,
               p.left_source_identity, p.right_source_identity,
               l.full_name AS left_name, r.full_name AS right_name,
               l.date_of_birth AS left_dob, r.date_of_birth AS right_dob
        FROM mdm.match_pair p
        JOIN (SELECT run_id FROM mdm.resolution_run
              ORDER BY started_at DESC LIMIT 1) latest USING (run_id)
        LEFT JOIN mdm.person l ON l.person_id = p.left_person_id AND l.is_current
        LEFT JOIN mdm.person r ON r.person_id = p.right_person_id AND r.is_current
        WHERE p.zone = 'GREY' AND p.decided_by {} 'STEWARD'
        ORDER BY p.score DESC LIMIT %s
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query.format("<>"), (100,))
        pairs = [dict(r) for r in cur.fetchall()]

        cur.execute(query.format("="), (25,))
        reviewed = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT count(*) AS n FROM mdm.standardization_rule WHERE state = 'SHADOW'"
        )
        pending_rules = int((cur.fetchone() or {}).get("n") or 0)

    body = [f"<h2>Ambiguous pairs · {len(pairs)} awaiting you</h2>"]
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
            "the model was asked about. Its verdict is advisory; yours is final. "
            "<strong>Outcome</strong> is what the last run actually did — your "
            "decision is recorded now and takes effect the next time the batch is "
            "processed, so a merge you approve does not silently rewrite the "
            "customer book underneath whoever is reading it.</p>"
        )
        body.append("<table><thead><tr><th>Score</th><th>Model</th><th>Left</th>"
                    "<th>Right</th><th>Outcome</th><th>Blocked on</th><th></th>"
                    "</tr></thead><tbody>")
        for pair in pairs:
            verdict = pair["ai_decision"] or "—"
            ai_score = "" if pair["ai_score"] is None else f"{pair['ai_score']:.2f}"
            # Three outcomes, not two. A pair the model rejected can still end
            # up merged, because connected components joined the two sides
            # through some *other* pair. Labelling that "merged" next to a
            # NO_MATCH verdict reads as a contradiction and sends the steward
            # looking for a bug; naming it is also the only way they can tell a
            # transitive merge from a direct one, which is what they would want
            # to review.
            merged = pair["left_person_id"] == pair["right_person_id"]
            if not merged:
                outcome = '<span class="zone-AUTO_REJECT">kept apart</span>'
            elif pair["final_decision"] == "MATCH":
                outcome = '<span class="zone-AUTO_MATCH">merged</span>'
            else:
                outcome = (
                    '<span class="zone-GREY">merged via another pair</span>'
                )
            action = (
                f'<form method="post" action="/console/steward/decide">'
                f'<input type="hidden" name="pair_id" value="{pair["pair_id"]}">'
                f'<input name="reason" placeholder="Reason" required size="16">'
                f'<button name="decision" value="MERGE">Merge</button> '
                f'<button class="secondary" name="decision" value="SEPARATE">'
                f"Separate</button></form>"
            )
            body.append(
                f'<tr><td class="zone-GREY">{pair["score"]:.3f}</td>'
                f'<td><span class="chip">{_esc(verdict)}</span> '
                f"{ai_score}</td>"
                f"<td>{_person_cell(pair, 'left')}</td>"
                f"<td>{_person_cell(pair, 'right')}</td>"
                f"<td>{outcome}</td>"
                f'<td><span class="chip">{_esc(pair["blocking_key"])}</span></td>'
                f"<td>{action}</td></tr>"
            )
        body.append("</tbody></table>")

    if reviewed:
        body.append(
            f"<h2>Decided by a steward · {len(reviewed)}</h2>"
            '<p class="note">Recorded, and read as a fixed edge the next time '
            "resolution runs — so the review is spent once rather than every "
            "night. Re-process the batch from the "
            '<a href="/console/ingest">ingestion console</a> to apply them.</p>'
            "<table><thead><tr><th>Score</th><th>Left</th><th>Right</th>"
            "<th>Your call</th></tr></thead><tbody>"
        )
        for pair in reviewed:
            body.append(
                f'<tr><td class="zone-GREY">{pair["score"]:.3f}</td>'
                f"<td>{_person_cell(pair, 'left')}</td>"
                f"<td>{_person_cell(pair, 'right')}</td>"
                f'<td><span class="chip">you: {_esc(pair["final_decision"])}</span>'
                "</td></tr>"
            )
        body.append("</tbody></table>")

    return _page("Steward queue", principal, "".join(body), active="steward")

def _person_cell(pair: dict[str, Any], side: str) -> str:
    """One side of a candidate pair.

    Falls back to the source identity when there is no golden record to link to.
    That happens for every pair the run merged — both sides became one person —
    and a blank cell there would hide exactly the pairs most worth checking.
    """
    person_id = pair[f"{side}_person_id"]
    name = pair[f"{side}_name"]
    identity = (pair[f"{side}_source_identity"] or "").replace("\x1f", " · ")
    label = _esc(name) if name else '<span class="masked">no golden record</span>'
    linked = f'<a href="/console/person/{person_id}">{label}</a>' if person_id else label
    return (
        f"{linked}<br><small class=\"masked\">{_esc(pair[f'{side}_dob'])} · "
        f"{_esc(identity)}</small>"
    )


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

    ``decided_by`` records *how*, not *who*: the next run finds the overrides by
    querying for the literal ``STEWARD``, and it cannot do that if the column
    holds a different username for every reviewer. Who decided, and the reason
    they gave, are in ``steward_action``.
    """
    _guard(principal, Action.MERGE, conn)
    if decision not in ("MERGE", "SEPARATE"):
        raise HTTPException(status_code=400, detail="decision must be MERGE or SEPARATE")

    from cmdm.resolve import STEWARD

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT left_person_id, right_person_id, left_source_identity, "
            "right_source_identity, score, zone, ai_decision "
            "FROM mdm.match_pair WHERE pair_id = %s",
            (pair_id,),
        )
        pair = cur.fetchone()
    if pair is None:
        raise HTTPException(status_code=404, detail="pair not found")

    record_steward_action(
        conn, principal,
        Action.MERGE if decision == "MERGE" else Action.SPLIT,
        entity_name="MatchPair", entity_id=pair_id,
        related_id=pair["left_person_id"], reason=reason,
        before={"zone": pair["zone"], "score": float(pair["score"]),
                "ai_decision": pair["ai_decision"],
                "left": pair["left_source_identity"],
                "right": pair["right_source_identity"]},
        after={"decision": decision},
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mdm.match_pair SET final_decision = %s, decided_by = %s "
            "WHERE pair_id = %s",
            ("MATCH" if decision == "MERGE" else "NO_MATCH", STEWARD, pair_id),
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
        body.append(
            '<p class="note">No rules proposed yet. The miner reads the AI '
            "fallback log offline rather than on the ingest path — run "
            "<code>python -m cmdm.worker mine</code> after a batch has been "
            "processed, and anything it finds appears here for approval.</p>"
        )
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

