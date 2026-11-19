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


def script(offer: dict[str, object], price_label: object = "7 246 735 ₽") -> str:
    entry = {
        "key": "defaultState",
        "value": {"offerData": {"offer": offer, "offerPrice": price_label}},
    }
    return (
        "window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        + json.dumps([entry])
        + ");"
    )


def projection_html(
    offer: dict[str, object],
    product: dict[str, object],
    price_label: object = "7 246 735 ₽",
) -> str:
    return (
        f"<script>{script(offer, price_label)}</script>"
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
    )


def native_case(name: str) -> tuple[str, dict[str, object], dict[str, object]]:
    path = FIXTURE.with_name("cian-native-directions.json")
    cases = cast(list[dict[str, object]], json.loads(path.read_text())["cases"])
    case = next(item for item in cases if item["case"] == name)
    return (
        cast(str, case["url"]),
        cast(dict[str, object], case["offer"]),
        cast(dict[str, object], case["product"]),
    )
