"""Normalize verified Avito card data to P3."""

import re
from decimal import Decimal
from html import unescape
from typing import Literal, cast
from unicodedata import normalize

from catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPhotos,
    KnownPrice,
    SnapshotKey,
    Unavailable,
    is_database_text,
    is_http_url,
)
from collection.normalization import (
    RejectedCard,
    as_object,
    coordinate,
    decimal_2,
    number,
    optional_integer,
)

_FLAT, _ROOM, _HOUSE = "kvartiry", "komnaty", "doma_dachi_kottedzhi"
_TYPES = {_FLAT: 1, _ROOM: 2, _HOUSE: 3}
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
}
_ADDRESS_ABBREVIATION = re.compile(
    r"(?<!\S)(" + "|".join(map(re.escape, _ABBREVIATIONS)) + r")(?=\s|,|$)",
    re.IGNORECASE,
)
_PUBLISHED_PRICE = re.compile(
    r"(от )?([0-9][0-9 ,.]*) ?₽(?: (в месяц|за (?:1 )?сутки))?"
)
_PRICE_PERIOD = {"sale": None, "month_rent": "в месяц", "day_rent": "за сутки"}
_AREA = re.compile(r"(.+?)\s+(м²|сот\.|га)")

_HOTEL_UNIT = re.compile(
    r"\b(?:гостиничный номер|номер в (?:отеле|гостинице|апарт-отеле))\b"
    r"|^номер\s+.+\bв (?:отеле|гостинице|апарт-отеле)\b"
)
_ROOM_TITLE_AREA = re.compile(
    r"^(?:доля: )?(?:(?:одна|1) )?комната ([0-9]+(?:[,.][0-9]+)?) м²(?: |,|$)"
)
_EXPLICIT_ONE_ROOM = re.compile(r"^(?:доля: )?(?:одна|1) комната\b")
_OTHER_OFFERED_ROOM = re.compile(r"\bи (?:ещ[её] )?(?:(?:одна|1) )?комната\b")


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


def _area(value: str | None, *, land: bool = False) -> Decimal | None:
    if value is None:
        return None
    match = _AREA.fullmatch(value)
    if match is None or (not land and match[2] != "м²"):
        raise RejectedCard("invalid_area_unit")
    scale = {"м²": 0, "сот.": 2, "га": 4}[match[2]]
    sign, digits, exponent = number(match[1]).as_tuple()
    assert isinstance(exponent, int)
    return decimal_2(
        Decimal((sign, digits, exponent + scale)),
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12" if land else "1e10"),
    )


def _address(item: dict[str, object]) -> tuple[str, int]:
    raw = item.get("address")
    if not is_database_text(raw) or not raw.strip():
        raise RejectedCard("invalid_address")
    address = _ADDRESS_ABBREVIATION.sub(
        lambda match: _ABBREVIATIONS[match[0].casefold()],
        normalize("NFC", _text(raw)),
    )
    subject = address.split(",", 1)[0].casefold()
    location = as_object(item.get("location"))
    if subject not in ("самарская область", "самара") and not (
        location.get("slug") == "samara"
        and item.get("locationId") == 653040
        and subject.startswith("улица ")
    ):
        raise RejectedCard("unconfirmed_avito_region")
    return address, 68


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
    if not all(is_http_url(url) for url in urls):
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
        if family == _ROOM and "Площадь комнаты" in params:
            if params.get("Условия продажи") == "Продажа доли":
                if not (
                    item_title.startswith("доля: комната ")
                    or _EXPLICIT_ONE_ROOM.match(item_title)
                ):
                    raise RejectedCard("unconfirmed_room_unit")
                return "sale", None
            if (
                item_title.startswith("комната ")
                or _EXPLICIT_ONE_ROOM.match(item_title)
            ) and (offer == "Продам" or "на продажу" in page_title):
                return "sale", None
        if (
            family == _HOUSE
            and item_title.startswith("дом ")
            and "Площадь дома" in params
            and (offer == "Продам" or "на продажу" in page_title)
        ):
            return "sale", None
    raise RejectedCard("unconfirmed_avito_card_family")


def normalize_avito_card(
    family: str,
    key: SnapshotKey,
    url: str,
    buyer: dict[str, object],
    title: str,
    expected_deal: Deal | None,
    listing_label: str | None,
) -> ActiveSnapshot:
    """Normalize an active card after the adapter has verified its identity."""
    item = as_object(buyer["item"])
    params = _params(buyer)
    deal, segment = _classify(family, item, buyer, params, title)
    if expected_deal is not None and expected_deal != deal:
        raise RejectedCard("unexpected_card_deal")
    address, region = _address(item)
    lat, lon = _point(item)
    area_label = {
        _FLAT: "Общая площадь",
        _ROOM: "Площадь комнаты",
        _HOUSE: "Площадь дома",
    }
    area = _area(params.get(area_label[family]))
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
    room_count = params.get("Количество комнат")
    if family == _ROOM:
        item_title = _text(item.get("title")).casefold()
        rooms = None
        if not _OTHER_OFFERED_ROOM.search(item_title):
            title_area = _ROOM_TITLE_AREA.match(item_title)
            if title_area and number(title_area[1]) != area:
                raise RejectedCard("conflicting_room_area")
            if _EXPLICIT_ONE_ROOM.match(item_title) or (
                title_area and params.get("Условия продажи") != "Продажа доли"
            ):
                rooms = 1
    elif family == _FLAT and room_count == "Студия":
        rooms = 0
    else:
        rooms = optional_integer(room_count, minimum=1)
    description = item.get("description")
    if description is not None and not is_database_text(description):
        raise RejectedCard("invalid_description")
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
        "land_area_m2": _area(params.get("Площадь участка"), land=True)
        if family == _HOUSE
        else None,
        "description": description or None,
        "price": _price(item, deal, listing_label),
        "photos": _photos(item),
    }
