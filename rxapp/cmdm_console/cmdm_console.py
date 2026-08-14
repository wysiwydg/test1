"""The Reflex application: pages, routes and the global style.

Kept deliberately thin. Everything it wires together lives in a module that can
be read on its own -- the state, the layout shell, the design tokens and each
page -- because an app file that also holds three pages is the file every change
touches and nobody reads.
"""

from __future__ import annotations

import reflex as rx

from .pages import customer_page, login_page, search_page
from .state import CustomerState
from .theme import GLOBAL_STYLE

app = rx.App(style=GLOBAL_STYLE)

app.add_page(search_page, route="/", title="Customers · Customer MDM")
app.add_page(login_page, route="/login", title="Sign in · Customer MDM")
app.add_page(
    customer_page,
    route="/customer/[customer_id]",
    title="Customer · Customer MDM",
    on_load=CustomerState.load_customer,
)
