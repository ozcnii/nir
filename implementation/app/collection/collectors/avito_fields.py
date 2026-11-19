"""Normalize verified Avito card data to P3."""

import re
from decimal import Decimal
from html import unescape
from typing import Literal, cast
from unicodedata import normalize

from app.catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPhotos,
    KnownPrice,
    SnapshotKey,
    Unavailable,
    is_database_text,
    is_photo_url,
)
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    REGION_NAMES,
    ROOM_TYPE_ID,
    SAMARA_REGION_ID,
)
from app.collection.collectors.avito_units import area_m2, check_house, rooms_and_area
from app.collection.normalization import (
    RejectedCard,
    as_object,
    coordinate,
    decimal_2,
    optional_integer,
)

_FLAT, _ROOM, _HOUSE = "kvartiry", "komnaty", "doma_dachi_kottedzhi"
_TYPES = {_FLAT: APARTMENT_TYPE_ID, _ROOM: ROOM_TYPE_ID, _HOUSE: HOUSE_TYPE_ID}
_AVITO_SAMARA_CITY_ID = 653040
_REGION_BY_NAME = {name.casefold(): key for key, name in REGION_NAMES.items()}
_LABELS = {
    "Количество комнат",
    "Общая площадь",
    "Площадь комнаты",
    "Этаж",
    "Площадь дома",
    "Площадь участка",
    "Этажей в доме",
    "Условия продажи",
}
_ABBREVIATIONS = {
    "обл.": "область",
    "ул.": "улица",
    "р-н": "район",
    "стр.": "строение",
    "пр-т": "проспект",
    "просп.": "проспект",
    "пер.": "переулок",
    "пос.": "посёлок",
    "кв.": "квартира",
    "корп.": "корпус",
    "под.": "подъезд",
    "комн.": "комната",
}
_ADDRESS_ABBREVIATION = re.compile(
    r"(?<!\S)(" + "|".join(map(re.escape, _ABBREVIATIONS)) + r")(?=\s|,|$)",
    re.IGNORECASE,
)
_PUBLISHED_PRICE = re.compile(
    r"(от )?([0-9][0-9 ,.]*) ?₽(?: (в месяц|за (?:1 )?сутки))?"
)
_PRICE_PERIOD = {"sale": None, "month_rent": "в месяц", "day_rent": "за сутки"}
_HOTEL_UNIT = re.compile(
    r"\b(?:гостиничный номер|номер в (?:отеле|гостинице|апарт-отеле))\b"
    r"|^номер\s+.+\bв (?:отеле|гостинице|апарт-отеле)\b"
)


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _params(buyer: dict[str, object]) -> dict[str, str]:
    values = as_object(buyer.get("paramsDto")).get("items")
    if not isinstance(values, list):
        raise RejectedCard("invalid_card_params")
    result: dict[str, str] = {}
    for raw in cast(list[object], values):
        param = as_object(raw)
        label = _text(param.get("title"))
        if not label:
            raise RejectedCard("invalid_card_param")
        if label not in _LABELS:
            continue
        value = _text(param.get("description"))
        if not value or label in result:
            raise RejectedCard("invalid_card_param")
        result[label] = value
    return result


def _address(item: dict[str, object]) -> tuple[str, int]:
    raw = item.get("address")
    if not is_database_text(raw) or not raw.strip():
        raise RejectedCard("invalid_address")
    address = _ADDRESS_ABBREVIATION.sub(
        lambda match: _ABBREVIATIONS[match[0].casefold()],
        normalize("NFC", _text(raw)),
    )
    subject = address.split(",", 1)[0].casefold()
    if (region := _REGION_BY_NAME.get(subject)) is not None:
        return address, region
    location = as_object(item.get("location"))
    if (
        location.get("slug") == "samara"
        and item.get("locationId") == _AVITO_SAMARA_CITY_ID
        and (subject.startswith("улица ") or subject == "самара")
    ):
        return address, SAMARA_REGION_ID
    raise RejectedCard("unconfirmed_avito_region")


def _point(item: dict[str, object]) -> tuple[Decimal | None, Decimal | None]:
    geo = item.get("geo")
    if geo is not None and not isinstance(geo, dict):
        raise RejectedCard("invalid_geo")
    coords = as_object(geo).get("coords")
    if coords is None:
        return None, None
    result = as_object(coords)
    if not {"lat", "lng"} <= result.keys():
        raise RejectedCard("invalid_coordinates")
    lat, lon = result["lat"], result["lng"]
    if lat is None and lon is None:
        return None, None
    if lat is None or lon is None:
        raise RejectedCard("invalid_coordinate_pair")
    return coordinate(lat, 90), coordinate(lon, 180)


def _photos(item: dict[str, object]) -> KnownPhotos | Unavailable:
    gallery = item.get("imageUrls")
    if not isinstance(gallery, list):
        return {"status": "unavailable"}
    urls = [as_object(row).get("1280x960") for row in cast(list[object], gallery)]
    if not all(is_photo_url(url) for url in urls):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], urls)))}


def _price(
    item: dict[str, object],
    deal: Deal,
    listing_label: str | None,
) -> KnownPrice | Unavailable:
    formatted = as_object(item.get("formattedPrice"))
    if formatted.get("currencySign") != "₽" or formatted.get("isHasValue") is not True:
        return {"status": "unavailable"}
    display = formatted.get("formatedString")
    if not isinstance(display, str) or not display.strip():
        return {"status": "unavailable"}
    published = _PUBLISHED_PRICE.fullmatch(_text(unescape(display)).casefold())
    if published is None or published[3] != _PRICE_PERIOD[deal]:
        return {"status": "unavailable"}
    try:
        bounds = {"minimum": Decimal(0), "maximum": Decimal("1e14")}
        amount = decimal_2(item.get("price"), **bounds)
        if (
            decimal_2(formatted.get("value"), **bounds) != amount
            or decimal_2(published[2], **bounds) != amount
        ):
            return {"status": "unavailable"}
        kind: Literal["from", "listed"] = "listed"
        if published[1]:
            kind = "from"
        elif deal == "day_rent":
            listed = _PUBLISHED_PRICE.fullmatch(_text(listing_label).casefold())
            if listed is None or listed[3] not in ("за сутки", "за 1 сутки"):
                return {"status": "unavailable"}
            listing_amount = decimal_2(listed[2], **bounds)
            if listed[1]:
                kind = "from"
            elif listing_amount != amount:
                return {"status": "unavailable"}
    except RejectedCard:
        return {"status": "unavailable"}
    return {
        "status": "known",
        "price_rub": amount,
        "price_kind": kind,
        "currency": "RUB",
    }


def _classify(
    family: str,
    item: dict[str, object],
    buyer: dict[str, object],
    params: dict[str, str],
    title: str,
) -> tuple[Deal, Literal["new_build", "resale"] | None]:
    events = buyer.get("ga")
    classification = (
        as_object(events[1]) if isinstance(events, list) and len(events) > 1 else {}
    )
    offer = classification.get("offer_type")
    item_title = _text(item.get("title")).casefold()
    page_title = _text(title).casefold()
    if any(
        _HOTEL_UNIT.search(direction)
        for direction in (
            item_title,
            page_title.split("|", 1)[0],
            _text(classification.get("status")).casefold(),
        )
    ):
        raise RejectedCard("unsupported_hotel_unit")
    formatted = as_object(item.get("formattedPrice"))
    postfix = _text(formatted.get("postfix"))
    if family == _FLAT:
        if offer == "Продам" and classification.get("status") == "Квартира":
            if postfix:
                raise RejectedCard("conflicting_card_deal")
            if classification.get("type") == "Новостройка":
                return "sale", "new_build"
            if classification.get("type") == "Вторичка":
                return "sale", "resale"
            raise RejectedCard("unconfirmed_market_segment")
        if offer == "Сдам" and (
            re.search(r"\bквартира\b", item_title)
            or re.search(r"\bквартира\b", page_title.split("|", 1)[0])
        ):
            if postfix == "в месяц":
                return "month_rent", None
            if postfix == "за сутки":
                return "day_rent", None
            raise RejectedCard("unconfirmed_rent_period")
    if not postfix and offer in (None, "Продам"):
        if family == _ROOM and (
            params.get("Условия продажи") == "Продажа доли"
            or offer == "Продам"
            or "на продажу" in page_title
        ):
            return "sale", None
        if family == _HOUSE and (offer == "Продам" or "на продажу" in page_title):
            return "sale", None
    raise RejectedCard("unconfirmed_avito_card_family")


def normalize_avito_card(
    family: str,
    key: SnapshotKey,
    url: str,
    buyer: dict[str, object],
    title: str,
    listing_label: str | None,
) -> ActiveSnapshot:
    """Normalize an active card after the adapter has verified its identity."""
    item = as_object(buyer["item"])
    params = _params(buyer)
    deal, segment = _classify(family, item, buyer, params, title)
    address, region = _address(item)
    lat, lon = _point(item)
    area_label = {
        _FLAT: "Общая площадь",
        _ROOM: "Площадь комнаты",
        _HOUSE: "Площадь дома",
    }
    area = area_m2(params.get(area_label[family]))
    floor = None
    floors = None
    if family == _HOUSE:
        floors = optional_integer(params.get("Этажей в доме"), minimum=1)
    elif (floor_value := params.get("Этаж")) is not None:
        pair = floor_value.split(" из ")
        if len(pair) != 2:
            raise RejectedCard("invalid_floor_pair")
        floor = optional_integer(pair[0], minimum=-32768)
        floors = optional_integer(pair[1], minimum=1)
        if floor is not None and floors is not None and floor > floors:
            raise RejectedCard("floor_above_building")
    description = item.get("description")
    if description is not None and not is_database_text(description):
        raise RejectedCard("invalid_description")
    room_count = params.get("Количество комнат")
    share = params.get("Условия продажи") == "Продажа доли"
    title = _text(item.get("title"))
    land = (
        area_m2(params.get("Площадь участка"), land=True) if family == _HOUSE else None
    )
    if family == _ROOM:
        rooms, area = rooms_and_area(title, description or "", area, share=share)
    elif family == _HOUSE:
        check_house(title, description or "", area, land, share=share, address=address)
        rooms = optional_integer(room_count, minimum=1)
    elif family == _FLAT and room_count == "Студия":
        rooms = 0
    else:
        rooms = optional_integer(room_count, minimum=1)
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region,
        "property_type_id": _TYPES[family],
        "deal_type": deal,
        "market_segment": segment,
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "area_m2": area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": land,
        "description": description or None,
        "price": _price(item, deal, listing_label),
        "photos": _photos(item),
    }
