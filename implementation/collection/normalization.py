"""Scalar normalization shared by collection adapters."""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Literal, cast
from unicodedata import normalize

from catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPhotos,
    KnownPrice,
    PriceKind,
    SnapshotKey,
    Unavailable,
    is_database_text,
    is_http_url,
)


class RejectedCard(ValueError):
    """A recognized card has invalid required or structural data."""


_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]+)?)"
)


def number(value: object) -> Decimal:
    if isinstance(value, str):
        value = value.strip().replace("\u00a0", " ").replace("\u202f", " ")
        if not _NUMBER.fullmatch(value):
            raise RejectedCard("invalid_number")
        value = value.replace(" ", "").replace(",", ".")
    elif type(value) not in (int, float, Decimal):
        raise RejectedCard("invalid_number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RejectedCard("invalid_number") from error
    if not result.is_finite():
        raise RejectedCard("invalid_number")
    return result


def decimal_2(value: object, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    result = number(value)
    if not minimum <= result < maximum:
        raise RejectedCard("number_out_of_range")
    rounded = result.quantize(Decimal("0.01"))
    if rounded != result:
        raise RejectedCard("invalid_precision")
    return rounded


def coordinate(value: object, limit: int) -> Decimal:
    result = number(value)
    if not -limit <= result <= limit:
        raise RejectedCard("coordinate_out_of_range")
    return result.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_EVEN)


def optional_integer(value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    result = number(value)
    if not minimum <= result <= 32767 or result != result.to_integral_value():
        raise RejectedCard("invalid_integer")
    return int(result)


def as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _optional_object(value: object) -> dict[str, object]:
    if value is not None and not isinstance(value, dict):
        raise RejectedCard("invalid_structure")
    return as_object(value)


_ADDRESS_PREFIXES = {
    "ул.": "улица",
    "просп.": "проспект",
    "пер.": "переулок",
    "кв.": "квартира",
}
_ADDRESS_COMPONENT = re.compile(
    r"(?<!\w)(ул\.|просп\.|пер\.|кв\.)\s*(?=\w)", re.IGNORECASE
)


def cian_address_component(value: object) -> str:
    if not is_database_text(value) or not value.strip():
        raise RejectedCard("invalid_address")
    text = " ".join(normalize("NFC", value).split())
    return _ADDRESS_COMPONENT.sub(
        lambda match: _ADDRESS_PREFIXES[match[1].casefold()] + " ", text
    )


def cian_description(value: object) -> str | None:
    if value is not None and not is_database_text(value):
        raise RejectedCard("invalid_description")
    return value or None


def cian_coordinates(
    latitude: object, longitude: object
) -> tuple[Decimal | None, Decimal | None]:
    if latitude is None and longitude is None:
        return None, None
    if latitude is None or longitude is None:
        raise RejectedCard("invalid_coordinate_pair")
    return coordinate(latitude, 90), coordinate(longitude, 180)


def _price(
    product: dict[str, object],
    data: dict[str, object],
    offer_data: dict[str, object],
    deal: Deal,
) -> KnownPrice | Unavailable:
    product_offer = as_object(product.get("offers"))
    if (
        product_offer.get("@type") != "Offer"
        or product_offer.get("priceCurrency") != "RUB"
    ):
        return {"status": "unavailable"}
    bargain = as_object(offer_data.get("bargainTerms"))
    if bargain.get("currency") not in (None, "rur"):
        return {"status": "unavailable"}
    display = data.get("offerPrice")
    if display is not None and not isinstance(display, str):
        return {"status": "unavailable"}
    label = " ".join(display.casefold().split()) if isinstance(display, str) else ""
    unit = {"sale": "₽", "month_rent": "₽/мес.", "day_rent": "₽/сут."}[deal]
    if not label.endswith(unit):
        return {"status": "unavailable"}
    if deal == "month_rent" and bargain.get("paymentPeriod") not in (None, "monthly"):
        return {"status": "unavailable"}
    try:
        price = decimal_2(
            product_offer.get("price"), minimum=Decimal(0), maximum=Decimal("1e14")
        )
        stated = bargain.get("price")
        if (
            stated is not None
            and decimal_2(stated, minimum=Decimal(0), maximum=Decimal("1e14")) != price
        ):
            return {"status": "unavailable"}
        displayed_amount = label.removeprefix("от ").removesuffix(unit).strip()
        if number(displayed_amount) != price:
            return {"status": "unavailable"}
    except RejectedCard:
        return {"status": "unavailable"}
    kind: PriceKind = "from" if label.startswith("от ") else "listed"
    return {
        "status": "known",
        "price_rub": price,
        "price_kind": kind,
        "currency": "RUB",
    }


def _photos(
    product: dict[str, object], offer: dict[str, object]
) -> KnownPhotos | Unavailable:
    published = offer.get("photos")
    images = product.get("image")
    if published == [] and images is None:
        return {"status": "known", "urls": []}
    if (
        not isinstance(images, list)
        or not isinstance(published, list)
        or len(published) != len(images)
        or not all(is_http_url(url) for url in cast(list[object], images))
    ):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], images)))}


_CIAN_REGIONS = {
    (4608, "Самарская область"): 68,
    (4609, "Саратовская область"): 69,
}
_M2_MAX = Decimal("1e10")
_SALE_ROOM_AREA = re.compile(
    r"(?:^|[.!?]\s+|\n\s*)Прода[её]тся\s+комната\s+"
    r"([0-9]+(?:[.,][0-9]+)?)\s*м²(?=\s|[.,;!?]|$)",
    re.IGNORECASE,
)


def cian_area(value: object) -> Decimal | None:
    if value is None:
        return None
    return decimal_2(value, minimum=Decimal("0.01"), maximum=_M2_MAX)


def _land_area(offer: dict[str, object]) -> Decimal | None:
    land = _optional_object(offer.get("land"))
    if land.get("area") is None:
        return None
    unit = land.get("areaUnitType")
    if unit != "sotka":
        raise RejectedCard("invalid_land_unit")
    value = number(land["area"])
    # Adjust the Decimal exponent before validating the two stored decimals.
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    return cian_area(Decimal((sign, digits, exponent + 2)))


def normalize_cian_card(
    url: str,
    key: SnapshotKey,
    data: dict[str, object],
    product: dict[str, object],
    *,
    deal: Deal,
    property_type_id: int,
    market_segment: Literal["new_build", "resale"] | None,
) -> ActiveSnapshot:
    offer = as_object(data.get("offer"))
    geo = _optional_object(offer.get("geo"))
    address = geo.get("address")
    if not isinstance(address, list):
        raise RejectedCard("invalid_region")
    components = [as_object(item) for item in cast(list[object], address)]
    subjects = [
        part
        for part in components
        if part.get("type") == "location" and part.get("locationTypeId") == 2
    ]
    if len(subjects) == 1:
        subject_id = subjects[0].get("id")
        subject_name = subjects[0].get("fullName")
        if not isinstance(subject_id, int) or not isinstance(subject_name, str):
            raise RejectedCard("invalid_region")
        region_id = _CIAN_REGIONS.get((subject_id, subject_name))
    elif not subjects and any(
        part.get("type") == "location"
        and part.get("locationTypeId") == 1
        and part.get("id") == 4966
        and part.get("fullName") == "Самара"
        for part in components
    ):
        region_id = 68
    else:
        region_id = None
    if region_id is None:
        raise RejectedCard("invalid_region")
    names = [cian_address_component(part.get("fullName")) for part in components]

    coordinates = _optional_object(geo.get("coordinates"))
    latitude, longitude = cian_coordinates(
        coordinates.get("lat"), coordinates.get("lng")
    )
    house = property_type_id == 3
    room = property_type_id == 2
    floor = (
        None if house else optional_integer(offer.get("floorNumber"), minimum=-32768)
    )
    floors = optional_integer(
        _optional_object(offer.get("building")).get("floorsCount"), minimum=1
    )
    if floor is not None and floors is not None and floor > floors:
        raise RejectedCard("floor_above_building")
    rooms = optional_integer(
        offer.get("roomsForSaleCount" if room else "roomsCount"),
        minimum=1 if house or room else 0,
    )
    if rooms == 0:
        raise RejectedCard("unconfirmed_studio")
    area = cian_area(offer.get("roomArea" if room else "totalArea"))
    description = cian_description(offer.get("description"))
    if room and deal == "sale" and rooms == 1 and area is not None and description:
        # Only an explicit sentence about the offered room can contradict roomArea.
        if any(
            number(match[1]) != area for match in _SALE_ROOM_AREA.finditer(description)
        ):
            raise RejectedCard("conflicting_room_area")

    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id,
        "property_type_id": property_type_id,
        "deal_type": deal,
        "market_segment": market_segment,
        "address": ", ".join(names),
        "latitude": latitude,
        "longitude": longitude,
        "area_m2": area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": _land_area(offer) if house else None,
        "description": description,
        "price": _price(product, data, offer, deal),
        "photos": _photos(product, offer),
    }


_ROOM_NAME = re.compile(r"\bномер\b|\b\d+\s+кроватью\b", re.IGNORECASE)
_PARTNER_DAILY = re.compile(
    r"\bЦена\s+(?P<from>от\s+)?(?P<amount>[0-9][0-9., \u00a0\u202f]*)"
    r"\s+RUB\s+за сутки\b",
    re.IGNORECASE,
)


def cian_partner_property_type(unit: dict[str, object]) -> int:
    kind = as_object(unit.get("type")).get("name")
    hotel = _optional_object(unit.get("hotel"))
    labels = (unit.get("name"), unit.get("title"))
    number_label = any(
        isinstance(label, str) and _ROOM_NAME.search(label) for label in labels
    )
    if kind == "room" and hotel and number_label:
        raise RejectedCard("hotel_accommodation")
    if kind == "apartment" and not number_label:
        return 1
    if (
        kind == "room"
        and not hotel
        and not number_label
        and unit.get("title") == "Комната"
    ):
        return 2
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
        if not is_http_url(url):
            return {"status": "unavailable"}
        urls.append(url)
    return {"status": "known", "urls": list(dict.fromkeys(urls))}


def _partner_properties(groups: object) -> dict[str, object]:
    if not isinstance(groups, list):
        raise RejectedCard("invalid_partner_properties")
    fields: dict[str, object] = {}
    for item in cast(list[object], groups):
        group = _optional_object(item)
        name = group.get("name")
        if name not in ("basic_properties", "detail_description"):
            continue
        properties = group.get("properties")
        if not isinstance(properties, list):
            raise RejectedCard("invalid_partner_properties")
        for row in cast(list[object], properties):
            prop = _optional_object(row)
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


def _partner_region(documents: list[dict[str, object]], city: str) -> tuple[int, str]:
    breadcrumbs = [
        doc.get("itemListElement")
        for doc in documents
        if doc.get("@type") == "BreadcrumbList"
    ]
    names: list[str] = []
    if len(breadcrumbs) == 1 and isinstance(breadcrumbs[0], list):
        crumbs = [as_object(item) for item in cast(list[object], breadcrumbs[0])]
        names = [
            name
            for crumb in crumbs
            if crumb.get("position") == 2 and isinstance(name := crumb.get("name"), str)
        ]
    if not names and not breadcrumbs and city == "Самара":
        return 68, "Самарская область"
    if len(names) == 1:
        for (_, name), region_id in _CIAN_REGIONS.items():
            if names[0] == name:
                return region_id, name
    raise RejectedCard("invalid_region")


def normalize_cian_partner(
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
    city = cian_address_component(location.get("city"))
    region_id, region = _partner_region(documents, city)
    names = [region, city]
    if location.get("address") is not None:
        names.append(cian_address_component(location["address"]))
    latitude, longitude = cian_coordinates(location.get("lat"), location.get("lng"))
    fields = _partner_properties(groups)
    floor = optional_integer(fields.get("floor"), minimum=-32768)
    floors = optional_integer(fields.get("max_floor"), minimum=1)
    if floor is not None and floors is not None and floor > floors:
        raise RejectedCard("floor_above_building")
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id,
        "property_type_id": property_type_id,
        "deal_type": "day_rent",
        "market_segment": None,
        "address": ", ".join(names),
        "latitude": latitude,
        "longitude": longitude,
        "area_m2": cian_area(fields.get("area")),
        # For an ordinary room the partner's rooms value may count interior rooms.
        "rooms": optional_integer(fields.get("rooms"), minimum=1)
        if property_type_id == 1
        else None,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": None,
        "description": cian_description(fields.get("description")),
        "price": _partner_prices(key, price_context, product),
        "photos": _partner_photos(images),
    }
