"""Load the saved Cian card projection and build its script fixture."""

import json
from pathlib import Path
from typing import cast

FIXTURE = Path(__file__).with_name("fixtures") / "cian-330631159.json"


def projection() -> tuple[str, dict[str, object], dict[str, object]]:
    data = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    return (
        cast(str, data["url"]),
        cast(dict[str, object], data["offer"]),
        cast(dict[str, object], data["product"]),
    )


def script(offer: dict[str, object]) -> str:
    entry = {"key": "defaultState", "value": {"offerData": {"offer": offer}}}
    return (
        "window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        + json.dumps([entry])
        + ");"
    )


def projection_html(offer: dict[str, object], product: dict[str, object]) -> str:
    return (
        f"<script>{script(offer)}</script>"
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
    )
