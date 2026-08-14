"""The single customer view: one party and everything the store links to it.

The page this replaces showed a golden record, its lineage and its household.
That answers "what do we believe about this person" and leaves the question
operators actually arrive with unanswered: *what is this party connected to?*

A party in an insurance book is a node in a graph, and every panel here is one
kind of edge leaving it. The policy panels carry the counterparties inline --
who else is on that contract, and in what role -- because "Ruby Edwards has five
policies" is a fact about a row, while "two of them are cover on her daughter,
written by an agent who also services her mother's policy" is the thing somebody
picked up the phone to find out.

Ordering is by how often a question is asked, not by how the data is stored:
identity, then what they hold, then who they are connected to, then why the
record says what it says, and last what the matcher considered. The match ledger
is last because it is the most specialised panel and the only one whose absence
would not be noticed on a normal day -- and it is present at all because "why
are these two *not* the same person" has no other answer.
"""

from __future__ import annotations

import reflex as rx

from ..layout import panel_grid, scroll_x, shell
from ..state import CustomerState
from ..theme import ZONE_COLOR, card, chip, metric, section_title


def _identity() -> rx.Component:
    return card(
        rx.vstack(
            rx.hstack(
                rx.heading(CustomerState.name, size="6", color="var(--fg)"),
                chip(CustomerState.party_type),
                rx.foreach(
                    CustomerState.flags,
                    lambda f: chip(f["label"], color=f["tone"]),
                ),
                spacing="3",
                align="center",
                wrap="wrap",
            ),
            rx.text(CustomerState.person_id, class_name="id", color="var(--muted)"),
            rx.cond(
                CustomerState.merged_notice != "",
                rx.callout(
                    CustomerState.merged_notice,
                    icon="info",
                    size="1",
                    margin_top="8px",
                    width="100%",
                ),
                rx.fragment(),
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
    )


def _stats() -> rx.Component:
    return card(
        rx.box(
            metric(CustomerState.stat_policies, "policy roles"),
            metric(CustomerState.stat_cover, "cover held"),
            metric(CustomerState.stat_household, "household"),
            metric(CustomerState.stat_sources, "source keys"),
            metric(CustomerState.stat_contested, "contested", tone="var(--warn)"),
            style={
                "display": "grid",
                # 150px, not 110: a sum assured runs to nine characters at 24px
                # and the narrower track let "7,755,000" and the next figure
                # collide into one unreadable number on a phone.
                "grid_template_columns": "repeat(auto-fit, minmax(150px, 1fr))",
                "gap": "18px 20px",
                "width": "100%",
            },
        ),
    )


def _empty(message: str) -> rx.Component:
    """What a panel says when it has nothing.

    A table header with no rows under it reads as a page that failed to load.
    Saying "nothing was compared against this party" is a finding; an empty
    table is an ambiguity the reader has to resolve by guessing.
    """
    return rx.text(message, size="1", color="var(--muted)")


def _policy_row(policy) -> rx.Component:
    """One policy, with the other parties on it directly underneath.

    Deliberately not a bare table row. The counterparties are the reason this
    panel exists, and a second table beside the first would make the reader join
    them by policy number by eye.
    """
    return rx.box(
        rx.hstack(
            rx.vstack(
                rx.hstack(
                    rx.link(
                        policy["policy_number"],
                        href="/entities",
                        weight="bold",
                        color="var(--accent)",
                    ),
                    chip(policy["role"]),
                    chip(policy["status"]),
                    rx.cond(
                        policy["also"] != "",
                        chip(f"also {policy['also']}", color="var(--muted)"),
                        rx.fragment(),
                    ),
                    spacing="2",
                    align="center",
                    wrap="wrap",
                ),
                rx.text(policy["product"], size="1", color="var(--muted)"),
                spacing="1",
                align="start",
                min_width="0",
            ),
            rx.spacer(),
            rx.vstack(
                rx.text(policy["cover"], size="2", weight="medium",
                        style={"font_variant_numeric": "tabular-nums"}),
                rx.text(policy["effective"], size="1", color="var(--muted)"),
                spacing="1",
                align="end",
            ),
            width="100%",
            align="start",
            spacing="3",
        ),
        rx.hstack(
            rx.icon("users", size=13, color="var(--muted)"),
            rx.text(policy["with_whom"], size="1", color="var(--muted)"),
            spacing="2",
            align="center",
            margin_top="6px",
            wrap="wrap",
        ),
        style={
            "padding": "10px 0",
            "border_bottom": "1px solid var(--line)",
            "width": "100%",
        },
    )


def _policy_panel(group) -> rx.Component:
    return card(
        section_title(group["label"]),
        rx.text(group["hint"], size="1", color="var(--muted)", margin_bottom="8px"),
        rx.foreach(
            CustomerState.policies,
            lambda p: rx.cond(
                p["group"] == group["group"], _policy_row(p), rx.fragment()
            ),
        ),
    )


def _household() -> rx.Component:
    return card(
        section_title("Household"),
        rx.cond(
            CustomerState.household_id != "",
            rx.vstack(
                rx.text(
                    "Built from the relationships the sources stated at "
                    "application, never from a shared address. Two people at one "
                    "postcode are two people.",
                    size="1", color="var(--muted)",
                ),
                rx.foreach(
                    CustomerState.household,
                    lambda m: rx.hstack(
                        rx.link(m["name"], href=f"/customer/{m['person_id']}",
                                color="var(--accent)"),
                        chip(m["relation"]),
                        rx.spacer(),
                        rx.text(m["evidence"], size="1", color="var(--muted)"),
                        width="100%",
                        align="center",
                        wrap="wrap",
                        spacing="2",
                        style={"padding": "7px 0",
                               "border_bottom": "1px solid var(--line)"},
                    ),
                ),
                spacing="2",
                width="100%",
                align="start",
            ),
            rx.text(
                "No household on record. Nothing the sources delivered links "
                "this party to another as family.",
                size="1", color="var(--muted)",
            ),
        ),
    )


def _affiliations() -> rx.Component:
    return card(
        section_title("Belongs to"),
        rx.text(
            "Companies, trusts and estates. Deliberately not part of the "
            "household — an employer is not family.",
            size="1", color="var(--muted)", margin_bottom="6px",
        ),
        rx.cond(
            CustomerState.affiliations.length() > 0,
            rx.foreach(
                CustomerState.affiliations,
                lambda a: rx.hstack(
                    rx.link(a["name"], href=f"/customer/{a['person_id']}",
                            color="var(--accent)"),
                    chip(a["party_type"]),
                    chip(a["relation"]),
                    rx.spacer(),
                    rx.text(a["evidence"], size="1", color="var(--muted)"),
                    width="100%", align="center", wrap="wrap", spacing="2",
                    style={"padding": "7px 0",
                           "border_bottom": "1px solid var(--line)"},
                ),
            ),
            rx.text("No affiliation on record.", size="1", color="var(--muted)"),
        ),
    )


def _table(headers: list[str], rows, cell) -> rx.Component:
    """A table that scrolls rather than squeezes.

    Without the min-width the columns compress to fit the card and the last
    header truncates mid-word -- "Confide". A clipped header is worse than a
    scrollbar: the reader cannot tell what the column holds, and nothing on the
    page says it was cut.
    """
    return scroll_x(
        rx.table.root(
            rx.table.header(
                rx.table.row(*[rx.table.column_header_cell(h) for h in headers])
            ),
            rx.table.body(rx.foreach(rows, cell)),
            variant="surface",
            size="1",
            style={"min_width": f"{max(360, 100 * len(headers))}px",
                   "width": "100%"},
        )
    )


def _sources() -> rx.Component:
    return card(
        section_title("Contributing sources"),
        rx.text(
            "Every source key that resolves to this party. This is the "
            "crosswalk — the record itself has no natural key.",
            size="1", color="var(--muted)", margin_bottom="8px",
        ),
        rx.cond(
            CustomerState.sources.length() > 0,
            _table(
            ["System", "Kind", "Key", "Active", "How", "Confidence"],
            CustomerState.sources,
            lambda s: rx.table.row(
                rx.table.cell(s["system"]),
                rx.table.cell(s["kind"]),
                rx.table.cell(s["key"], class_name="id"),
                rx.table.cell(s["active"]),
                rx.table.cell(chip(s["how"])),
                rx.table.cell(s["confidence"]),
            ),
            ),
            _empty("No source key resolves to this party, which should not be "
                   "possible for a record that exists."),
        ),
    )


def _lineage() -> rx.Component:
    return card(
        section_title("Why these values"),
        rx.cond(
            CustomerState.lineage.length() > 0,
            rx.fragment(
                rx.text(
                    "Only attributes the sources disagreed about appear here. "
                    "Everything else was uncontested.",
                    size="1", color="var(--muted)", margin_bottom="8px",
                ),
                _table(
                    ["Attribute", "Surviving value", "Rule", "Won from", "Candidates"],
                    CustomerState.lineage,
                    lambda e: rx.table.row(
                        rx.table.cell(e["attribute"]),
                        rx.table.cell(e["value"]),
                        rx.table.cell(chip(e["strategy"])),
                        rx.table.cell(e["won_from"]),
                        rx.table.cell(e["candidates"]),
                    ),
                ),
            ),
            rx.text(
                "No contested attributes: every source that contributed to this "
                "record agreed.",
                size="1", color="var(--muted)",
            ),
        ),
    )


def _considered() -> rx.Component:
    return card(
        section_title("What the matcher considered"),
        rx.text(
            "Parties compared against this one, merged or not. The rejections "
            "are kept deliberately: “why are these two not the same person” is "
            "asked as often as the opposite, and the golden record has no memory "
            "of a party it was decided not to be.",
            size="1", color="var(--muted)", margin_bottom="8px",
        ),
        rx.cond(
            CustomerState.considered.length() > 0,
            _table(
            ["Other party", "Score", "Zone", "Decision", "Decided by", "Model", "AI"],
            CustomerState.considered,
            lambda c: rx.table.row(
                rx.table.cell(c["other"], class_name="id"),
                rx.table.cell(c["score"],
                              style={"font_variant_numeric": "tabular-nums"}),
                rx.table.cell(
                    rx.text(c["zone"], size="1",
                            color=ZONE_COLOR.get("GREY", "var(--muted)"))
                ),
                rx.table.cell(c["decision"]),
                rx.table.cell(chip(c["decided_by"])),
                rx.table.cell(c["model"]),
                rx.table.cell(c["ai_score"]),
            ),
            ),
            _empty("Blocking never generated a candidate pair for this party — "
                   "no other record shared a name, email, phone or address key "
                   "with it, so nothing was ever compared."),
        ),
    )


def _versions() -> rx.Component:
    return card(
        section_title("Version history"),
        rx.text(
            "A new version exists only where a business field changed. Audit "
            "columns moving does not manufacture one.",
            size="1", color="var(--muted)", margin_bottom="8px",
        ),
        _table(
            ["Version", "From", "To"],
            CustomerState.versions,
            lambda v: rx.table.row(
                rx.table.cell(v["version"]),
                rx.table.cell(v["valid_from"]),
                rx.table.cell(v["valid_to"]),
            ),
        ),
    )


def customer_page() -> rx.Component:
    return shell(
        rx.cond(
            CustomerState.denied,
            rx.callout(
                "You are signed in, but your roles do not include reading a "
                "customer record.",
                icon="lock", color_scheme="red",
            ),
            rx.cond(
                CustomerState.not_found,
                rx.callout("No such party.", icon="circle-alert"),
                rx.vstack(
                    _identity(),
                    _stats(),
                    # The policy panels come first among the linkage, because
                    # what a party holds is what most calls are about.
                    rx.foreach(CustomerState.policy_groups, _policy_panel),
                    panel_grid(_household(), _affiliations()),
                    panel_grid(_sources(), _versions()),
                    _lineage(),
                    _considered(),
                    spacing="3",
                    width="100%",
                ),
            ),
        ),
    )
