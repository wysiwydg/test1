"""Reflex project configuration for the Customer MDM console.

The app lives beside the wheel rather than inside it. Reflex resolves an app by
importing ``<app_name>.<app_name>``, which fixes the module layout of anything
it owns; putting that shape inside ``src/cmdm/`` would let a frontend framework
dictate the package structure of the domain code, and the domain code is the
part that has to outlive the framework. ``cmdm_console`` imports from ``cmdm``
and never the other way round, so the MDM system does not depend on Reflex at
all -- it is not in the runtime closure, and the FastAPI console still runs with
Reflex absent.

**On building this offline.** Reflex compiles a Vite/React frontend, which needs
Node and roughly 200 MB of npm packages. None of that can exist on the target.
It does not have to: the compile happens where the bundle is *built*, and the
output is about 650 KB of static HTML, JS and CSS. The target serves those files
and runs the Python backend, which needs no Node at all -- verified by running
it with Node removed from PATH.
"""

import reflex as rx
from reflex_base.plugins.sitemap import SitemapPlugin

config = rx.Config(
    app_name="cmdm_console",
    # The API the compiled frontend talks to. Same host as the rest of the
    # system, so a bundle that reaches the console reaches this too.
    api_url="http://127.0.0.1:8100",
    frontend_port=3100,
    backend_port=8100,
    plugins=[
        # Radix supplies component behaviour and sizing; the colours come from
        # the tokens in theme.py, so both consoles agree about what a match zone
        # looks like. `appearance="inherit"` is what makes the page follow the
        # operator's own light/dark setting rather than picking one for them.
        rx.plugins.RadixThemesPlugin(
            theme=rx.theme(appearance="inherit", accent_color="blue",
                           radius="medium"),
        ),
    ],
    # A sitemap is for a public site being crawled. This one runs on a machine
    # with no internet.
    disable_plugins=[SitemapPlugin],
    # Telemetry is off because this system is deployed on machines with no
    # internet and, more to the point, holds insurance customer data. A
    # framework phoning home from inside that boundary is not something to
    # leave at its default.
    telemetry_enabled=False,
    # The framework's sticky badge. Off because this is an internal operations
    # console for insurance staff, not a showcase -- and it overlaps the content
    # in the bottom-right corner of every page.
    show_built_with_reflex=False,
)
