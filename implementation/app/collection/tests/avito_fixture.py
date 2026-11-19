"""Minimal projections from the Avito 6.1 public field audit."""

import json
from html import escape
from pathlib import Path
from typing import TypedDict, cast

# The published position of card 4595685717, FINAL61-STATUS.md.
DAILY_LISTING_LABEL = "от 4 980 ₽ за 1 сутки"

_FILE = Path(__file__).with_name("fixtures") / "avito-6.1.json"


class Projection(TypedDict):
    url: str
    title: str
    route: dict[str, object]


def projection(label: str) -> Projection:
    return cast(dict[str, Projection], json.loads(_FILE.read_text(), parse_float=str))[
        label
    ]


def projection_html(card: Projection) -> str:
    data = {"loaderData": {"catalog-or-main-or-item": card["route"]}}
    hydration = json.dumps(data).replace("<", r"\u003c")
    return (
        f"<title>{escape(card['title'])}</title><script>"
        f"window.__staticRouterHydrationData = {hydration};</script>"
    )
