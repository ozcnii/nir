"""Normalize the verified Nuxt entity of a Cian daily partner card."""

import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.parse import urlsplit

from app.catalog.contracts import (
    ActiveSnapshot,
    KnownPhotos,
    KnownPrice,
    PriceKind,
    SnapshotKey,
    Unavailable,
    is_photo_url,
    is_publication_url,
)
from app.catalog.reference_defaults import APARTMENT_TYPE_ID
from app.collection.collectors.cian_fields import (
    ROOM_TYPE_ID,
    RejectedCard,
    address_component,
    area_m2,
    as_object,
    check_floor,
    check_room,
    coordinates,
    decimal_2,
    number,
    optional_integer,
    region_id,
    seller_description,
)
from app.collection.collectors.nuxt_state import UNDEFINED, resolve_reference

PARTNER_HOST = "cian.sutochno.ru"
_PARTNER_PATH = re.compile(r"/detail/([1-9][0-9]*)/?")


def partner_key(url: str) -> str | None:
    if not is_publication_url(url):
        return None
    parts = urlsplit(url)
    if parts.hostname != PARTNER_HOST or parts.port not in (
        None,
        443 if parts.scheme == "https" else 80,
    ):
        return None
    match = _PARTNER_PATH.fullmatch(parts.path)
    return f"/detail/{match[1]}" if match else None


class _Nuxt:
    """Resolve only the indexed Nuxt values this adapter reads."""

    def __init__(self, body: str) -> None:
        try:
            rows = json.loads(body, parse_float=Decimal)
        except (ValueError, InvalidOperation, RecursionError) as error:
            raise RejectedCard("invalid_partner_state") from error
        if not isinstance(rows, list) or not rows:
            raise RejectedCard("invalid_partner_state")
        self.rows = cast(list[object], rows)
        self.remaining = 30_000

    def _value(self, ref: object) -> object:
        self.remaining -= 1
        if self.remaining < 0:
            raise RejectedCard("invalid_partner_reference")
        try:
            value = resolve_reference(
                self.rows, ref, ("Reactive", "ShallowReactive"), max_hops=10
            )
        except ValueError as error:
            raise RejectedCard("invalid_partner_reference") from error
        if value is UNDEFINED:
            raise RejectedCard("invalid_partner_reference")
        return value

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
            route = partner_key(identity)
            page_url = identity.split("#", 1)[0].split("?", 1)[0]
            if page_url != internal_url and (route != expected_key):
                return {}
    return product


def parse_partner(
    url: str,
    key: SnapshotKey,
    scripts: Iterable[str],
    documents: list[dict[str, object]],
) -> ActiveSnapshot | None:
    payloads = list(scripts)
    if len(payloads) != 1:
        return None
    identifier = key["external_id"].removeprefix("/detail/")
    path = ("data", f"detail:{identifier}")
    try:
        nuxt = _Nuxt(payloads[0])
        actual = nuxt.read(*path, "id")
        if type(actual) is not int or str(actual) != identifier:
            return None
        internal_url = f"https://sutochno.ru/front/searchapp/detail/{identifier}"
        if nuxt.read(*path, "url") != internal_url:
            return None
        if nuxt.read(*path, "status", "name") != "approved":
            return None
    except RejectedCard:
        return None

    # Inspect type/parent before reading structural fields or constructing P3.
    property_type_id = _property_type(
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
    location["city_id"] = nuxt.read(*path, "location", "city", "id")
    try:
        images = nuxt.read(*path, "media", "images")
    except RejectedCard:
        images = None
    return _snapshot(
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


_ROOM_NAME = re.compile(r"\bномер\b|\b\d+\s+кроватью\b", re.IGNORECASE)
_PARTNER_DAILY = re.compile(
    r"\bЦена\s+(?P<from>от\s+)?(?P<amount>[0-9][0-9., \u00a0\u202f]*)"
    r"\s+RUB\s+за сутки\b",
    re.IGNORECASE,
)


def _property_type(unit: dict[str, object]) -> int:
    kind = as_object(unit.get("type")).get("name")
    hotel = unit.get("hotel")
    if hotel is not None and not isinstance(hotel, dict):
        raise RejectedCard("invalid_partner_structure")
    labels = (unit.get("name"), unit.get("title"))
    number_label = any(
        isinstance(label, str) and _ROOM_NAME.search(label) for label in labels
    )
    if kind == "room" and hotel and number_label:
        raise RejectedCard("hotel_accommodation")
    if kind == "apartment" and not number_label:
        return APARTMENT_TYPE_ID
    if (
        kind == "room"
        and not hotel
        and not number_label
        and unit.get("title") == "Комната"
    ):
        return ROOM_TYPE_ID
    raise RejectedCard("ambiguous_partner_unit")


def _partner_prices(
    key: SnapshotKey, context: dict[str, object], product: dict[str, object]
) -> KnownPrice | Unavailable:
    guests = as_object(context.get("guests"))
    base = as_object(context.get("base"))
    base_price = as_object(base.get("base_price"))
    offer = as_object(product.get("offers"))
    object_id = base.get("object_id")
    if (
        "checkIn" not in context
        or "checkOut" not in context
        or context["checkIn"] is not None
        or context["checkOut"] is not None
        or guests.get("adults") != 2
        or guests.get("childrens") != []
        or as_object(guests.get("pets")).get("value") is not False
        or type(object_id) is not int
        or f"/detail/{object_id}" != key["external_id"]
        or context.get("currency") != "RUB"
        or base_price.get("currency_code") != "RUB"
        or offer.get("@type") != "AggregateOffer"
        or offer.get("priceCurrency") != "RUB"
    ):
        return {"status": "unavailable"}
    text = product.get("description")
    labels = list(_PARTNER_DAILY.finditer(text)) if isinstance(text, str) else []
    if len(labels) != 1:
        return {"status": "unavailable"}
    try:
        price = decimal_2(
            base_price.get("price"), minimum=Decimal(0), maximum=Decimal("1e14")
        )
        if price != number(labels[0]["amount"]) or price != number(
            offer.get("lowPrice")
        ):
            return {"status": "unavailable"}
    except RejectedCard:
        return {"status": "unavailable"}
    kind: PriceKind = "from" if labels[0]["from"] else "listed"
    return {
        "status": "known",
        "price_rub": price,
        "price_kind": kind,
        "currency": "RUB",
    }


def _partner_photos(images: object) -> KnownPhotos | Unavailable:
    if not isinstance(images, list):
        return {"status": "unavailable"}
    urls: list[str] = []
    for item in cast(list[object], images):
        url = as_object(item).get("path")
        if not isinstance(url, str):
            return {"status": "unavailable"}
        if url.startswith("//"):
            url = f"https:{url}"
        if not is_photo_url(url):
            return {"status": "unavailable"}
        urls.append(url)
    return {"status": "known", "urls": list(dict.fromkeys(urls))}


def _partner_properties(groups: object) -> dict[str, object]:
    if not isinstance(groups, list):
        raise RejectedCard("invalid_partner_properties")
    fields: dict[str, object] = {}
    for item in cast(list[object], groups):
        group = as_object(item)
        name = group.get("name")
        if name not in ("basic_properties", "detail_description"):
            continue
        properties = group.get("properties")
        if not isinstance(properties, list):
            raise RejectedCard("invalid_partner_properties")
        for row in cast(list[object], properties):
            prop = as_object(row)
            field = prop.get("name")
            if not isinstance(field, str):
                raise RejectedCard("invalid_partner_properties")
            if (
                name == "detail_description"
                and field != "description"
                or name == "basic_properties"
                and field not in ("area", "rooms", "floor", "max_floor")
            ):
                continue
            if field in fields or "value" not in prop:
                raise RejectedCard("invalid_partner_properties")
            if field == "area" and prop.get("title") != "Площадь (м2)":
                raise RejectedCard("invalid_area_unit")
            fields[field] = prop["value"]
    return fields


# The observed partner identity differs from Cian's native city code.
_SAMARA_CITY_ID = 366625
_SAMARA_CITY = "самара"
_SAMARA_REGION = "Самарская область"


def _partner_region(
    documents: list[dict[str, object]], city: str, city_id: object
) -> str:
    known_city_name = city.casefold() == _SAMARA_CITY
    known_city_id = type(city_id) is int and city_id == _SAMARA_CITY_ID
    if city_id is not None and known_city_name != known_city_id:
        raise RejectedCard("conflicting_region")
    breadcrumbs = [
        document.get("itemListElement")
        for document in documents
        if document.get("@type") == "BreadcrumbList"
    ]
    if not breadcrumbs and known_city_id:
        return _SAMARA_REGION
    if len(breadcrumbs) != 1 or not isinstance(breadcrumbs[0], list):
        raise RejectedCard("invalid_region")
    crumbs = [as_object(item) for item in cast(list[object], breadcrumbs[0])]
    subjects = [crumb.get("name") for crumb in crumbs if crumb.get("position") == 2]
    cities = [crumb.get("name") for crumb in crumbs if crumb.get("position") == 3]
    if len(subjects) != 1 or len(cities) > 1:
        raise RejectedCard("invalid_region")
    region = address_component(subjects[0])
    if (
        known_city_name
        and region.casefold() != _SAMARA_REGION.casefold()
        or cities
        and address_component(cities[0]).casefold() != city.casefold()
    ):
        raise RejectedCard("conflicting_region")
    return region


def _snapshot(
    url: str,
    key: SnapshotKey,
    property_type_id: int,
    location: dict[str, object],
    groups: object,
    images: object,
    price_context: dict[str, object],
    documents: list[dict[str, object]],
    product: dict[str, object],
) -> ActiveSnapshot:
    city = address_component(location.get("city"))
    region = _partner_region(documents, city, location.get("city_id"))
    names = [region, city]
    if location.get("address") is not None:
        names.append(address_component(location["address"]))
    latitude, longitude = coordinates(location.get("lat"), location.get("lng"))
    fields = _partner_properties(groups)
    floor = optional_integer(fields.get("floor"), minimum=-32768)
    floors = optional_integer(fields.get("max_floor"), minimum=1)
    check_floor(floor, floors)
    area = area_m2(fields.get("area"))
    description = seller_description(fields.get("description"))
    # The partner's rooms field may count rooms inside the listed unit.
    rooms = (
        optional_integer(fields.get("rooms"), minimum=1)
        if property_type_id == APARTMENT_TYPE_ID
        else None
    )
    if property_type_id == ROOM_TYPE_ID:
        rooms, area = check_room(rooms, area, description)
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id(region),
        "property_type_id": property_type_id,
        "deal_type": "day_rent",
        "market_segment": None,
        "address": ", ".join(names),
        "latitude": latitude,
        "longitude": longitude,
        "area_m2": area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": None,
        "description": description,
        "price": _partner_prices(key, price_context, product),
        "photos": _partner_photos(images),
    }
