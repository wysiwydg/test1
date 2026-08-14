"""The shell every page sits in: a navigation rail and a fluid content grid.

The server-rendered console is a single fixed column capped at 1180px. That is
the right shape for a document and the wrong one for a 360 view, where the
question is "what is this party connected to" and the answer is eight panels
that want to be read together. On a wide monitor the old layout showed one panel
and a lot of empty margin; the operator scrolled instead of comparing.

So the content area is a grid that reflows on the container rather than on a
device class:

    wide      [ rail ][ panel ][ panel ][ panel ]
    medium    [ rail ][ panel ][ panel ]
    narrow    [ ─── rail as a strip ─── ]
              [ panel ]
              [ panel ]

There is no separate mobile template. One set of components, one breakpoint,
and the panels decide their own minimum width -- which is what keeps a table of
policies readable at 900px instead of squeezed into a column that fits nothing.
"""

from __future__ import annotations

import reflex as rx

from .state import AuthState
from .theme import NARROW

#: Every destination in the console, with the permission it needs. Kept as data
#: rather than as a hand-written list of links so the rail cannot drift from
#: what the pages actually authorize -- a link to a page that will refuse you is
#: worse than no link.
NAV: list[tuple[str, str, str, str]] = [
    ("/", "Customers", "users", "SEARCH"),
    ("/entities", "Entities", "database", "SEARCH"),
    ("/export", "Export", "download", "EXPORT"),
    ("/ingest", "Ingestion", "upload", "SUBMIT"),
    ("/steward", "Steward queue", "scale", "MERGE"),
    ("/rules", "Learned rules", "book-open", "APPROVE_RULE"),
    ("/quality", "Quality", "activity", "SEARCH"),
]


def _nav_item(href: str, label: str, icon: str, action: str) -> rx.Component:
    active = AuthState.active_route == href
    return rx.cond(
        AuthState.allowed.contains(action),
        rx.link(
            rx.hstack(
                rx.icon(icon, size=15),
                rx.text(label, size="2"),
                spacing="2",
                align="center",
                width="100%",
            ),
            href=href,
            style={
                "display": "block",
                "padding": "7px 10px",
                "border_radius": "7px",
                "text_decoration": "none",
                "white_space": "nowrap",
                "color": rx.cond(active, "var(--accent)", "var(--fg)"),
                "background": rx.cond(active, "var(--chip)", "transparent"),
                "font_weight": rx.cond(active, "600", "400"),
                "_hover": {"background": "var(--chip)"},
            },
        ),
        rx.fragment(),
    )


def _rail() -> rx.Component:
    """Navigation. A column beside the content when there is room, a wrapping
    strip above it when there is not -- never a hamburger menu hiding seven
    destinations behind a tap, on a console whose users move between them
    constantly."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.icon("network", size=18, color="var(--accent)"),
                rx.text("Customer MDM", weight="bold", size="2"),
                spacing="2",
                align="center",
                padding="2px 8px 10px",
            ),
            rx.box(
                *[_nav_item(*item) for item in NAV],
                style={
                    "display": "flex",
                    "flex_direction": "column",
                    "gap": "2px",
                    "width": "100%",
                    # Below the breakpoint the same items become a wrapping row.
                    f"@media (max-width: {NARROW})": {
                        "flex_direction": "row",
                        "flex_wrap": "wrap",
                    },
                },
            ),
            rx.spacer(),
            rx.vstack(
                rx.text(AuthState.subject, size="1", color="var(--muted)"),
                rx.text(AuthState.roles_label, size="1", color="var(--muted)"),
                spacing="0",
                align="start",
                padding="10px 8px 0",
                style={f"@media (max-width: {NARROW})": {"display": "none"}},
            ),
            spacing="0",
            height="100%",
            align="start",
            width="100%",
        ),
        style={
            "grid_area": "rail",
            "border_right": "1px solid var(--line)",
            "padding": "16px 10px",
            "position": "sticky",
            "top": "0",
            "align_self": "start",
            "max_height": "100vh",
            "overflow_y": "auto",
            f"@media (max-width: {NARROW})": {
                "border_right": "0",
                "border_bottom": "1px solid var(--line)",
                "position": "static",
                "max_height": "none",
            },
        },
    )


def shell(*content, title: str = "") -> rx.Component:
    """Wrap page content in the rail and the content column."""
    return rx.box(
        _rail(),
        rx.box(
            *content,
            style={
                "grid_area": "main",
                "padding": "20px 24px 60px",
                "min_width": "0",
                f"@media (max-width: {NARROW})": {"padding": "16px 14px 40px"},
            },
        ),
        style={
            "display": "grid",
            "grid_template_columns": "210px minmax(0, 1fr)",
            "grid_template_areas": '"rail main"',
            "min_height": "100vh",
            "background": "var(--bg)",
            "color": "var(--fg)",
            f"@media (max-width: {NARROW})": {
                "grid_template_columns": "minmax(0, 1fr)",
                "grid_template_areas": '"rail" "main"',
            },
        },
    )


def panel_grid(*panels, min_width: str = "340px") -> rx.Component:
    """Panels that reflow to fill the width, however wide it is.

    `auto-fit` with a `minmax` track is what makes this responsive to the
    *container* rather than to a guessed set of device widths: the browser fits
    as many columns as will hold `min_width` and stretches them to fill. Adding
    a panel needs no breakpoint, and neither does an operator dragging the
    window narrower.
    """
    return rx.box(
        *panels,
        style={
            "display": "grid",
            "grid_template_columns": f"repeat(auto-fit, minmax({min_width}, 1fr))",
            "gap": "14px",
            "width": "100%",
            "align_items": "start",
        },
    )


def scroll_x(*children) -> rx.Component:
    """A wide table, scrolling inside its own panel.

    Ten columns of an entity are wider than the column of text the rest of the
    console is set in. Letting the page scroll sideways instead would move the
    navigation off screen with it.
    """
    return rx.box(
        *children,
        style={"overflow_x": "auto", "width": "100%", "max_width": "100%"},
    )
