"""Read the public Cian card's published inline state."""

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import (
    ActiveSnapshot,
    Deal,
    Market,
    SnapshotKey,
    is_publication_url,
)
from app.catalog.reference_defaults import APARTMENT_TYPE_ID
from app.collection.collectors.cian_fields import (
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
    RejectedCard,
    as_object,
    normalize_cian_card,
)
from app.collection.collectors.cian_partner import (
    PARTNER_HOST,
    parse_partner,
    partner_key,
)

_NATIVE_HOSTS = {
    "cian.ru",
    "www.cian.ru",
    "samara.cian.ru",
    "tolyatti.cian.ru",
    "saratov.cian.ru",
}
_CARD_PATH = re.compile(r"/(sale|rent)/(flat|suburban)/([1-9][0-9]{0,19})/?")
_CONFIG = re.compile(
    r"""_cianConfig\[['"]frontend-offer-card['"]\].*?\.concat\(\s*""", re.DOTALL
)


@dataclass(frozen=True)
class _Route:
    deal: str
    kind: str
    external_id: str


def _route(url: str) -> _Route | None:
    if not is_publication_url(url) or "\\" in url:
        return None
    parts = urlsplit(url)
    if parts.port not in (None, 443 if parts.scheme == "https" else 80):
        return None
    if parts.hostname == PARTNER_HOST and (key := partner_key(url)):
        return _Route("rent", "partner", key)
    if (
        parts.scheme == "https"
        and parts.hostname in _NATIVE_HOSTS
        and (native := _CARD_PATH.fullmatch(parts.path))
    ):
        return _Route(native[1], native[2], native[3])
    return None


def card_key(url: str) -> str | None:
    route = _route(url)
    return route.external_id if route else None


def same_card(requested_url: str, loaded_url: str) -> bool:
    requested = _route(requested_url)
    return requested is not None and requested == _route(loaded_url)


@dataclass(frozen=True)
class _Direction:
    deal: Deal
    property_type_id: int
    market: Market | None = None


_DIRECTIONS = {
    "newBuildingFlatSale": _Direction("sale", APARTMENT_TYPE_ID, "new_build"),
    "flatSale": _Direction("sale", APARTMENT_TYPE_ID, "resale"),
    "roomSale": _Direction("sale", ROOM_TYPE_ID),
    "houseSale": _Direction("sale", HOUSE_TYPE_ID),
    "flatRent": _Direction("month_rent", APARTMENT_TYPE_ID),
    "roomRent": _Direction("month_rent", ROOM_TYPE_ID),
    "cottageRent": _Direction("month_rent", HOUSE_TYPE_ID),
    "dailyFlatRent": _Direction("day_rent", APARTMENT_TYPE_ID),
    "dailyRoomRent": _Direction("day_rent", ROOM_TYPE_ID),
    "dailyHouseRent": _Direction("day_rent", HOUSE_TYPE_ID),
}


class _UnavailableCard(SnapshotKey):
    state: Literal["unavailable"]


class _RemovedCard(SnapshotKey):
    state: Literal["removed"]
    url: str
    deal_type: Deal


CianSnapshot = ActiveSnapshot | _RemovedCard | _UnavailableCard


def _state(scripts: Iterable[str], key: str) -> dict[str, object]:
    states = []
    removed_markers = []
    foreign_id = False
    for script in scripts:
        if not (marker := _CONFIG.search(script)):
            continue
        try:
            entries, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(
                script, marker.end()
            )
        except (ValueError, InvalidOperation, RecursionError):
            continue
        if isinstance(entries, list):
            for item in cast(list[object], entries):
                entry = as_object(item)
                if entry.get("key") == "defaultState":
                    data = as_object(as_object(entry.get("value")).get("offerData"))
                    offer = as_object(data.get("offer"))
                    identifier = offer.get("id")
                    if type(identifier) is int and str(identifier) == key:
                        states.append(data)
                    elif identifier is None and offer.get("status") == "deleted":
                        removed_markers.append(data)
                    elif identifier is not None or offer.get("cianId") is not None:
                        foreign_id = True
    if states:
        return states[0] if len(states) == 1 else {}
    return removed_markers[0] if not foreign_id and len(removed_markers) == 1 else {}


def _documents(scripts: Iterable[str]) -> list[dict[str, object]]:
    documents = []
    for script in scripts:
        try:
            document = as_object(json.loads(script, parse_float=Decimal))
        except (ValueError, InvalidOperation, RecursionError):
            continue
        if document:
            documents.append(document)
    return documents


def _product(
    documents: Iterable[dict[str, object]], route: _Route
) -> dict[str, object]:
    matches = []
    for product in documents:
        sku = product.get("sku")
        url = as_object(product.get("offers")).get("url")
        if (
            product.get("@type") == "Product"
            and type(sku) in (int, str)
            and str(sku) == route.external_id
            and isinstance(url, str)
            and _route(url) == route
        ):
            matches.append(product)
    return matches[0] if len(matches) == 1 else {}


def parse_card(
    requested_url: str,
    observed_at: datetime,
    config_scripts: Iterable[str],
    product_scripts: Iterable[str],
    *,
    loaded_url: str,
) -> CianSnapshot:
    route = _route(requested_url)
    if route is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    key: SnapshotKey = {
        "source_code": "cian",
        "external_id": route.external_id,
        "observed_at": observed_at,
    }
    unavailable: _UnavailableCard = {**key, "state": "unavailable"}
    if _route(loaded_url) != route:
        return unavailable
    documents = _documents(product_scripts)
    if route.kind == "partner":
        return parse_partner(loaded_url, key, config_scripts, documents) or unavailable

    data = _state(config_scripts, route.external_id)
    offer = as_object(data.get("offer"))
    identifier = offer.get("id")
    status = offer.get("status")
    secondary_id = offer.get("cianId", identifier)
    category = offer.get("category")
    direction = _DIRECTIONS.get(category) if isinstance(category, str) else None
    # A removed native sale can lose its active fields after publication ends.
    # The exact loaded path and explicit deleted marker still identify it.
    if (
        status == "deleted"
        and identifier is None
        and secondary_id is None
        and route.deal == "sale"
        and offer.get("offerType") in (None, route.kind)
        and offer.get("dealType") in (None, route.deal)
        and (
            category is None
            or (
                direction is not None
                and direction.deal == "sale"
                and (direction.property_type_id == HOUSE_TYPE_ID)
                == (route.kind == "suburban")
            )
        )
    ):
        return {**key, "state": "removed", "url": loaded_url, "deal_type": "sale"}
    if (
        status not in ("published", "deleted")
        or type(identifier) is not int
        or str(identifier) != route.external_id
        or type(secondary_id) is not int
        or secondary_id != identifier
        or offer.get("offerType") != route.kind
        or offer.get("dealType") != route.deal
    ):
        return unavailable
    if direction is None:
        raise RejectedCard("invalid_card_direction")
    if (direction.deal == "sale") != (route.deal == "sale") or (
        direction.property_type_id == HOUSE_TYPE_ID
    ) != (route.kind == "suburban"):
        return unavailable
    if status == "deleted":
        return {
            **key,
            "state": "removed",
            "url": loaded_url,
            "deal_type": direction.deal,
        }

    return normalize_cian_card(
        loaded_url,
        key,
        data,
        _product(documents, route),
        deal=direction.deal,
        property_type_id=direction.property_type_id,
        market_segment=direction.market,
    )


class _EmbeddedPage(TypedDict):
    url: str
    captcha: bool
    config: list[str]
    products: list[str]


def capture_card(
    page: Page, url: str, observed_at: datetime, deadline: float
) -> CianSnapshot:
    previous_config: list[str] = []
    while True:
        data = _wait_for_card_scripts(page, url, deadline, previous_config)
        if data["captcha"]:
            raise RejectedCard("source_captcha")
        snapshot = parse_card(
            url, observed_at, data["config"], data["products"], loaded_url=data["url"]
        )
        if (
            snapshot["state"] != "unavailable"
            or not same_card(url, data["url"])
            or urlsplit(url).hostname == PARTNER_HOST
            or _state(data["config"], snapshot["external_id"])
        ):
            return snapshot
        previous_config = data["config"]


def _wait_for_card_scripts(
    page: Page, url: str, deadline: float, previous_config: list[str]
) -> _EmbeddedPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
        r"""({url, partnerHost, previousConfig}) => {
            const trimSlash = path => path.replace(/\/$/, "");
            const expected = new URL(url);
            const partner = expected.hostname === partnerHost;
            const shifted = trimSlash(location.pathname) !== trimSlash(expected.pathname)
                || partner !== (location.hostname === partnerHost);
            const captcha = trimSlash(location.pathname) === "/cian-captcha"
                || document.title.toLowerCase().includes("captcha");
            if (!shifted && !captcha && document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(script => !script.src);
            const config = partner
                ? scripts.filter(script =>
                    script.id === "__NUXT_DATA__" && script.type === "application/json"
                ).map(script => script.textContent)
                : scripts.map(script => script.textContent).filter(text =>
                    text.includes("_cianConfig") && text.includes("frontend-offer-card")
                    && text.includes("defaultState") && text.includes("offerData")
                );
            const changed = config.length !== previousConfig.length
                || config.some((text, index) => text !== previousConfig[index]);
            if (!shifted && !captcha && !changed) return false;
            return {
                url: location.href, captcha, config,
                products: scripts
                    .filter(script => script.type === "application/ld+json")
                    .map(script => script.textContent),
            };
        }""",
        arg={
            "url": url,
            "partnerHost": PARTNER_HOST,
            "previousConfig": previous_config,
        },
        timeout=remaining_ms,
    )
    try:
        return cast(_EmbeddedPage, handle.json_value())
    finally:
        handle.dispose()
