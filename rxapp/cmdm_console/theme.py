"""Design tokens, in one place so a colour means the same thing on every page.

The palette is carried over from the server-rendered console rather than
reinvented. Two consoles for the same system that disagree about what "grey
zone" looks like teach operators to read the colour as decoration, and the whole
point of colouring a match zone is that it is *not* decoration -- amber is the
band a human is expected to look at.

Every value is defined for both light and dark. The console runs on an
operator's desktop all day; whichever they have set is the one it should honour.
"""

from __future__ import annotations

import reflex as rx

#: Semantic colours. Named for what they mean, never for what they are -- a
#: token called `amber` would have to be renamed the day the grey zone stops
#: being amber, and every call site would have to be found.
LIGHT = {
    "bg": "#ffffff",
    "panel": "#ffffff",
    "sunken": "#f6f8fa",
    "fg": "#1a1d21",
    "muted": "#5c6570",
    "line": "#dfe3e8",
    "accent": "#1c5d99",
    "good": "#1f7a44",
    "warn": "#a8620a",
    "bad": "#a32d2d",
    "chip": "#eef2f6",
    "ai": "#6d4aa8",
}

DARK = {
    "bg": "#14171a",
    "panel": "#1a1e23",
    "sunken": "#181c20",
    "fg": "#e8eaed",
    "muted": "#9aa4af",
    "line": "#2b3138",
    "accent": "#6fb3e8",
    "good": "#6cc48f",
    "warn": "#e0a355",
    "bad": "#e08585",
    "chip": "#1e242b",
    "ai": "#b79ce8",
}


def _block(selector: str, palette: dict[str, str]) -> dict[str, str]:
    return {f"--{name}": value for name, value in palette.items()}


#: Injected once at the app root. Tokens live on `:root` so a component can use
#: `var(--muted)` without importing anything, and the dark values are a
#: redefinition of the same names rather than a parallel set -- a component that
#: forgot to handle dark mode is impossible when there is only one name.
GLOBAL_STYLE = {
    ":root": _block(":root", LIGHT),
    "@media (prefers-color-scheme: dark)": {":root": _block(":root", DARK)},
    # An explicit choice wins over the system preference in both directions.
    ":root[data-theme='light']": _block(":root", LIGHT),
    ":root[data-theme='dark']": _block(":root", DARK),
    "body": {
        "background": "var(--bg)",
        "color": "var(--fg)",
        "margin": "0",
        "font_family": (
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
        ),
    },
    # A uuid is 36 characters. Left to wrap it triples the height of every row
    # it appears in, so ids are monospace and never wrap -- still selectable,
    # which truncating them would not be.
    ".id": {
        "font_family": "ui-monospace, Menlo, Consolas, monospace",
        "font_size": "11.5px",
        "white_space": "nowrap",
    },
}

#: The breakpoint the layout turns on. Below it the navigation rail becomes a
#: horizontal strip and every multi-column grid collapses to one column. One
#: number, used everywhere, so panels cannot disagree about when they are narrow.
NARROW = "62em"

ZONE_COLOR = {
    "AUTO_MATCH": "var(--good)",
    "GREY": "var(--warn)",
    "AUTO_REJECT": "var(--muted)",
}


def card(*children, **props) -> rx.Component:
    """A panel. The unit the whole layout is built from.

    Everything on a 360 page is one of these, which is what lets the page be a
    grid that reflows rather than a fixed column of sections: a card does not
    care how wide it is or what is beside it.
    """
    style = {
        "background": "var(--panel)",
        "border": "1px solid var(--line)",
        "border_radius": "10px",
        "padding": "16px 18px",
        "width": "100%",
        # Wide content -- a table of ten columns -- scrolls inside the card.
        # Without this the page itself scrolls sideways and takes the
        # navigation off screen with it.
        "min_width": "0",
    }
    style.update(props.pop("style", {}))
    return rx.box(*children, style=style, **props)


def section_title(text: str, hint: str | None = None) -> rx.Component:
    return rx.vstack(
        rx.heading(text, size="3", weight="bold", color="var(--fg)"),
        rx.cond(
            hint is not None,
            rx.text(hint or "", size="1", color="var(--muted)"),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        margin_bottom="10px",
        width="100%",
    )


def chip(text, color: str = "var(--fg)", background: str = "var(--chip)") -> rx.Component:
    return rx.box(
        text,
        style={
            "display": "inline-block",
            "padding": "1px 9px",
            "border_radius": "10px",
            "background": background,
            "color": color,
            "font_size": "12px",
            "white_space": "nowrap",
        },
    )


def metric(value, label: str, tone: str = "var(--fg)") -> rx.Component:
    """One number and what it counts.

    The label is never omitted, however obvious the number looks in context. A
    figure on a dashboard with no unit is the most confidently misread thing a
    console can show.
    """
    return rx.vstack(
        rx.text(value, size="6", weight="bold", color=tone,
                style={"font_variant_numeric": "tabular-nums", "line_height": "1.1"}),
        rx.text(label, size="1", color="var(--muted)",
                style={"text_transform": "uppercase", "letter_spacing": ".04em"}),
        spacing="1",
        align="start",
        min_width="0",
    )
