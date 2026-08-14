"""Customer search, and the sign-in that guards everything else."""

from __future__ import annotations

import reflex as rx

from ..layout import scroll_x, shell
from ..state import CustomerState
from ..theme import card, chip, section_title


def search_page() -> rx.Component:
    return shell(
        rx.vstack(
            card(
                section_title(
                    "Customers",
                    "Search the golden records by name. Matching is on the "
                    "normalized name, so punctuation and casing do not matter.",
                ),
                rx.form(
                    rx.hstack(
                        rx.input(
                            name="query",
                            placeholder="name…",
                            default_value=CustomerState.query,
                            width="100%",
                            max_width="360px",
                        ),
                        rx.button("Search", type="submit"),
                        spacing="2",
                        wrap="wrap",
                        width="100%",
                    ),
                    on_submit=CustomerState.search,
                ),
            ),
            rx.cond(
                CustomerState.results.length() > 0,
                card(
                    scroll_x(
                        rx.table.root(
                            rx.table.header(
                                rx.table.row(
                                    rx.table.column_header_cell("Name"),
                                    rx.table.column_header_cell("Type"),
                                    rx.table.column_header_cell("Date of birth"),
                                    rx.table.column_header_cell("Email"),
                                    rx.table.column_header_cell("Sources"),
                                )
                            ),
                            rx.table.body(
                                rx.foreach(
                                    CustomerState.results,
                                    lambda r: rx.table.row(
                                        rx.table.cell(
                                            rx.link(
                                                r["full_name"],
                                                href=f"/customer/{r['person_id']}",
                                                color="var(--accent)",
                                            )
                                        ),
                                        rx.table.cell(chip(r["party_type"])),
                                        rx.table.cell(r["date_of_birth"]),
                                        rx.table.cell(r["email_address"]),
                                        rx.table.cell(r["source_count"]),
                                    ),
                                )
                            ),
                            variant="surface",
                            size="1",
                            width="100%",
                        )
                    )
                ),
                rx.cond(
                    CustomerState.searched,
                    card(rx.text("No customer matched that name.", size="2",
                                 color="var(--muted)")),
                    rx.fragment(),
                ),
            ),
            spacing="3",
            width="100%",
        )
    )


def login_page() -> rx.Component:
    """Sign-in. Deliberately outside the shell — the rail would show
    destinations that will all refuse an unauthenticated caller."""
    return rx.center(
        card(
            rx.vstack(
                rx.heading("Customer MDM", size="5"),
                rx.text(
                    "Paste one of the API keys printed at start-up. Each key "
                    "carries a role, and the console shows only what that role "
                    "may reach.",
                    size="1", color="var(--muted)",
                ),
                rx.form(
                    rx.vstack(
                        rx.input(name="api_key", type="password",
                                 placeholder="API key", width="100%"),
                        rx.button("Sign in", type="submit", width="100%"),
                        spacing="2",
                        width="100%",
                    ),
                    on_submit=CustomerState.sign_in,
                    width="100%",
                ),
                rx.cond(
                    CustomerState.error != "",
                    rx.callout(CustomerState.error, icon="triangle-alert",
                               color_scheme="red", size="1", width="100%"),
                    rx.fragment(),
                ),
                spacing="3",
                width="100%",
            ),
            style={"max_width": "380px", "width": "100%"},
        ),
        height="100vh",
        padding="20px",
        background="var(--bg)",
    )
