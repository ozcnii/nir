"""Read the published state of native Cian and Cian's daily partner cards."""

import json
import re
import time
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Literal, NamedTuple, TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from catalog.contracts import Deal, Snapshot, SnapshotKey, is_http_url
from collection.browser import NAVIGATION_TIMEOUT_MS
from collection.normalization import (
    RejectedCard,
    as_object,
    cian_partner_property_type,
    normalize_cian_card,
    normalize_cian_partner,
)

_NATIVE_HOSTS = {
    "cian.ru",
    "www.cian.ru",
    "samara.cian.ru",
    "tolyatti.cian.ru",
    "saratov.cian.ru",
}
_PARTNER_HOST = "cian.sutochno.ru"
_CARD_PATH = re.compile(r"/(sale|rent)/(flat|suburban)/([1-9][0-9]{0,19})/?")
_PARTNER_PATH = re.compile(r"/detail/([1-9][0-9]*)/?")
CONFIG = re.compile(
    r"""_cianConfig\[['"]frontend-offer-card['"]\].*?\.concat\(\s*""", re.DOTALL
)


class _Route(NamedTuple):
    deal: str
    kind: str
    external_id: str


def _route(url: str) -> _Route | None:
    if not is_http_url(url) or "\\" in url:
        return None
    parts = urlsplit(url)
    if parts.port not in (None, 443 if parts.scheme == "https" else 80):
        return None
    if parts.hostname == _PARTNER_HOST and (
        partner := _PARTNER_PATH.fullmatch(parts.path)
    ):
        return _Route("rent", "partner", f"/detail/{partner[1]}")
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


class _Direction(NamedTuple):
    deal: Deal
    property_type_id: int
    market: Literal["resale", "new_build"] | None = None


_DIRECTIONS = {
    "newBuildingFlatSale": _Direction("sale", 1, "new_build"),
    "flatSale": _Direction("sale", 1, "resale"),
    "roomSale": _Direction("sale", 2),
    "houseSale": _Direction("sale", 3),
    "flatRent": _Direction("month_rent", 1),
    "roomRent": _Direction("month_rent", 2),
    "cottageRent": _Direction("month_rent", 3),
    "dailyFlatRent": _Direction("day_rent", 1),
    "dailyRoomRent": _Direction("day_rent", 2),
    "dailyHouseRent": _Direction("day_rent", 3),
}


def _state(scripts: Iterable[str]) -> dict[str, object]:
    found: dict[str, object] | None = None
    for script in scripts:
        if not (marker := CONFIG.search(script)):
            continue
        try:
            entries, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(
                script, marker.end()
            )
        except (ValueError, RecursionError):
            continue
        if not isinstance(entries, list):
            continue
        for item in cast(list[object], entries):
            entry = as_object(item)
            if entry.get("key") == "defaultState":
                if found is not None:
                    return {}
                found = as_object(as_object(entry.get("value")).get("offerData"))
    return found or {}


def _structured_data(scripts: Iterable[str]) -> list[dict[str, object]]:
    documents = []
    for script in scripts:
        try:
            document = as_object(json.loads(script, parse_float=Decimal))
        except (ValueError, RecursionError):
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
            and isinstance(sku, (int, str))
            and str(sku) == route.external_id
            and isinstance(url, str)
            and _route(url) == route
        ):
            matches.append(product)
    return matches[0] if len(matches) == 1 else {}


class _Nuxt:
    """Resolve only the indexed Nuxt values this adapter reads."""

    def __init__(self, body: str) -> None:
        try:
            rows = json.loads(body, parse_float=Decimal)
        except (ValueError, RecursionError) as error:
            raise RejectedCard("invalid_partner_state") from error
        if not isinstance(rows, list) or not rows:
            raise RejectedCard("invalid_partner_state")
        self.rows = cast(list[object], rows)
        self.remaining = 30_000

    def _value(self, ref: object) -> object:
        self.remaining -= 1
        if self.remaining < 0:
            raise RejectedCard("invalid_partner_reference")
        if type(ref) is not int or not 0 <= ref < len(self.rows):
            raise RejectedCard("invalid_partner_reference")
        value = self.rows[ref]
        for _ in range(10):
            if (
                not isinstance(value, list)
                or not value
                or value[0] not in ("Reactive", "ShallowReactive")
            ):
                return value
            index = value[1] if len(value) == 2 else None
            if type(index) is not int or not 0 <= index < len(self.rows):
                break
            value = self.rows[index]
        raise RejectedCard("invalid_partner_reference")

    def _decode(self, value: object, depth: int) -> object:
        if depth == 0:
            raise RejectedCard("invalid_partner_reference")
        if isinstance(value, dict):
            return {
                key: self._decode(self._value(ref), depth - 1)
                for key, ref in as_object(value).items()
                if ref != -1
            }
        if isinstance(value, list):
            return [
                self._decode(self._value(ref), depth - 1)
                for ref in cast(list[object], value)
            ]
        return value

    def read(self, *path: str, default: object = None) -> object:
        value = self._value(0)
        for key in path:
            if value is None:
                return default
            if not isinstance(value, dict):
                raise RejectedCard("invalid_partner_structure")
            ref = as_object(value).get(key, -1)
            if ref == -1:
                return default
            value = self._value(ref)
        return self._decode(value, 12)


def _partner_product(
    documents: list[dict[str, object]],
    images: object,
    expected_key: str,
    internal_url: str,
) -> dict[str, object]:
    products = [item for item in documents if item.get("@type") == "Product"]
    if len(products) != 1 or not isinstance(images, list) or not images:
        return {}
    product = products[0]
    # The observed Product has no card URL or SKU. Its image is the first image
    # of the verified Nuxt entity, which binds its tariff label to that entity.
    first_path = as_object(cast(list[object], images)[0]).get("path")
    if not isinstance(first_path, str) or product.get("image") != first_path:
        return {}

    for item in (product, as_object(product.get("offers"))):
        for field in ("sku", "productID", "identifier"):
            if field in item and str(item[field]) != expected_key.removeprefix(
                "/detail/"
            ):
                return {}
        for field in ("url", "@id", "mainEntityOfPage"):
            if field not in item:
                continue
            identity = item[field]
            if isinstance(identity, dict):
                identity = as_object(identity).get("@id")
            if not isinstance(identity, str):
                return {}
            route = _route(identity)
            page_url = identity.split("#", 1)[0].split("?", 1)[0]
            if page_url != internal_url and (
                route is None
                or route.kind != "partner"
                or route.external_id != expected_key
            ):
                return {}
    return product


def _parse_partner(
    url: str,
    key: SnapshotKey,
    scripts: Iterable[str],
    documents: list[dict[str, object]],
) -> Snapshot:
    unavailable: Snapshot = {**key, "state": "unavailable"}
    payloads = list(scripts)
    if len(payloads) != 1:
        return unavailable
    identifier = key["external_id"].removeprefix("/detail/")
    path = ("data", f"detail:{identifier}")
    try:
        nuxt = _Nuxt(payloads[0])
        actual = nuxt.read(*path, "id")
        if type(actual) is not int or str(actual) != identifier:
            return unavailable
        internal_url = f"https://sutochno.ru/front/searchapp/detail/{identifier}"
        if nuxt.read(*path, "url") != internal_url:
            return unavailable
        if nuxt.read(*path, "status", "name") != "approved":
            return unavailable
    except RejectedCard:
        return unavailable

    # Inspect type/parent before reading structural fields or constructing P3.
    property_type_id = cian_partner_property_type(
        {field: nuxt.read(*path, field) for field in ("type", "title", "name", "hotel")}
    )
    # Price is optional. Missing context differs from explicitly unselected dates.
    missing = object()
    try:
        search_path = ("vuex", "search")
        price_context = {
            "checkIn": nuxt.read(*search_path, "checkIn", default=missing),
            "checkOut": nuxt.read(*search_path, "checkOut", default=missing),
            "guests": nuxt.read(*search_path, "guests"),
            "base": nuxt.read("vuex", "detail", "objectBasePrice"),
            "currency": nuxt.read(*path, "currency", "code"),
        }
    except RejectedCard:
        price_context = {}
    location = {
        field: nuxt.read(*path, "location", field)
        for field in ("address", "lat", "lng")
    }
    location["city"] = nuxt.read(*path, "location", "city", "title")
    try:
        images = nuxt.read(*path, "media", "images")
    except RejectedCard:
        images = None
    return normalize_cian_partner(
        url,
        key,
        property_type_id,
        location,
        nuxt.read(*path, "propertyGroups"),
        images,
        price_context,
        documents,
        _partner_product(documents, images, key["external_id"], internal_url),
    )


def parse_card(
    requested_url: str,
    observed_at: datetime,
    config_scripts: Iterable[str],
    product_scripts: Iterable[str],
    *,
    loaded_url: str,
) -> Snapshot:
    route = _route(requested_url)
    if route is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    key: SnapshotKey = {
        "source_code": "cian",
        "external_id": route.external_id,
        "observed_at": observed_at,
    }
    unavailable: Snapshot = {**key, "state": "unavailable"}
    if _route(loaded_url) != route:
        return unavailable
    documents = _structured_data(product_scripts)
    if route.kind == "partner":
        return _parse_partner(loaded_url, key, config_scripts, documents)

    data = _state(config_scripts)
    offer = as_object(data.get("offer"))
    identifier = offer.get("id")
    status = offer.get("status")
    if status not in ("published", "deleted"):
        return unavailable
    secondary_id = offer.get("cianId", identifier)
    if (
        identifier is None
        and secondary_id is None
        and status == "deleted"
        and route.deal == "sale"
        and offer.get("offerType") in (None, route.kind)
        and offer.get("dealType") in (None, route.deal)
    ):
        return {**key, "state": "removed", "url": loaded_url, "deal_type": "sale"}
    if (
        type(identifier) is not int
        or str(identifier) != route.external_id
        or type(secondary_id) is not int
        or secondary_id != identifier
    ):
        return unavailable
    if offer.get("offerType") != route.kind or offer.get("dealType") != route.deal:
        return unavailable
    category = offer.get("category")
    direction = _DIRECTIONS.get(category) if isinstance(category, str) else None
    if direction is None:
        raise RejectedCard("invalid_card_direction")
    if (direction.deal == "sale") != (route.deal == "sale") or (
        direction.property_type_id == 3
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


class EmbeddedPage(TypedDict):
    url: str
    captcha: bool
    config: list[str]
    products: list[str]


def capture_page(page: Page, url: str, deadline: float) -> EmbeddedPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
        r"""requested => {
            const expected = new URL(requested);
            const trimSlash = path => path.replace(/\/$/, "");
            const partner = expected.hostname === "cian.sutochno.ru";
            const redirected = partner !== (location.hostname === "cian.sutochno.ru")
                || trimSlash(location.pathname) !== trimSlash(expected.pathname);
            const captcha = trimSlash(location.pathname) === "/cian-captcha"
                || document.title.toLowerCase().includes("captcha");
            // Bootstrap scripts can mention this card before its published data arrives.
            if (!redirected && !captcha && document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(script => !script.src);
            const config = partner
                ? scripts.filter(script =>
                    script.id === "__NUXT_DATA__" && script.type === "application/json"
                ).map(script => script.textContent)
                : scripts.map(script => script.textContent).filter(text =>
                    text.includes("_cianConfig") && text.includes("frontend-offer-card")
                    && text.includes("defaultState") && text.includes("offerData")
                );
            if (!redirected && !captcha && config.length === 0) return false;
            return {
                url: location.href, captcha, config,
                products: scripts
                    .filter(script => script.type === "application/ld+json")
                    .map(script => script.textContent),
            };
        }""",
        arg=url,
        timeout=remaining_ms,
    )
    try:
        data = cast(EmbeddedPage, handle.json_value())
    finally:
        handle.dispose()
    if time.monotonic() >= deadline:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    return data


def read_card(page: Page, requested_url: str, observed_at: datetime) -> Snapshot:
    """Read an already loaded page without separate media requests."""
    data = capture_page(
        page, requested_url, time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
    )
    return parse_card(
        requested_url,
        observed_at,
        data["config"],
        data["products"],
        loaded_url=data["url"],
    )
