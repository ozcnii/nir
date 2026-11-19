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
from app.collection.normalization import (
    RejectedCard,
    as_object,
    coordinate,
    decimal_2,
    number,
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
_AREA = re.compile(r"(.+?)\s+(м²|сот\.|га)")
_MEASUREMENT = r"[0-9]+(?:[.,][0-9]+)?"
_ROOM_TITLE = re.compile(
    r"(?:доля:\s*)?(?:(?P<count>[1-9][0-9]*|одна|две|три)\s+)?"
    r"(?P<unit>комната|комнаты|комнат)\b(?:\s+№\s*(?P<room>[1-9][0-9]*))?"
    rf"(?:\s+(?P<total>общей площадью )?(?P<area>{_MEASUREMENT})\s+м²)?"
)
_NEXT_ROOM = re.compile(r"\s+и\s+(?:ещ[её]\s+)?")
_SELLER_ROOM = re.compile(r"(?:прода[её]тся|сда[её]тся)\s+")
_HOUSE_TITLE = re.compile(
    rf"дом(?: (?P<area>{_MEASUREMENT}) м²)?"
    rf"(?: на участке (?P<land>{_MEASUREMENT} (?:м²|сот\.|га)))?"
)


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


def _room_advertised(
    title: str, position: int = 0
) -> tuple[int | None, Decimal | None, int]:
    match = _ROOM_TITLE.match(title, position)
    if match is None:
        return None, None, position
    count = match["count"]
    if count is None:
        rooms = 1 if match["unit"] == "комната" else None
    elif count.isdecimal():
        rooms = optional_integer(count, minimum=1)
    else:
        rooms = {"одна": 1, "две": 2, "три": 3}[count]
    area = (
        _area(f"{match['area']} м²")
        if match["area"] and (rooms == 1 or match["total"])
        else None
    )
    return rooms, area, match.end()


def _rooms_and_area(
    title: str, description: str, area: Decimal | None, *, share: bool
) -> tuple[int | None, Decimal | None]:
    label = title.casefold()
    rooms, title_area, end = _room_advertised(label)
    more = _NEXT_ROOM.match(label, end)
    if rooms is not None and more is not None:
        additional, _, _ = _room_advertised(label, more.end())
        if additional is not None:
            rooms += additional
            title_area = None
    if share or label.startswith("доля"):
        heading = _ROOM_TITLE.match(label)
        if (
            rooms != 1
            or heading is None
            or not label.startswith("доля:")
            or not (heading["room"] or heading["count"] in ("одна", "1"))
        ):
            raise RejectedCard("unconfirmed_room_unit")

    seller = description.casefold()
    introduction = _SELLER_ROOM.match(seller)
    seller_count, seller_area, _ = (
        _room_advertised(seller, introduction.end())
        if introduction
        else (None, None, 0)
    )
    if rooms is not None and seller_count is not None and rooms != seller_count:
        raise RejectedCard("conflicting_room_count")
    rooms = rooms if rooms is not None else seller_count
    if rooms is not None and rooms > 1:
        # The structured room-area field does not describe the entire set.
        area = None
    areas = {value for value in (area, title_area, seller_area) if value is not None}
    if len(areas) > 1:
        raise RejectedCard("conflicting_room_area")
    return rooms, next(iter(areas), None)


def _check_house(
    title: str, area: Decimal | None, land: Decimal | None, share: bool
) -> None:
    card = _HOUSE_TITLE.fullmatch(title.casefold())
    if card is None or share:
        raise RejectedCard("unconfirmed_house_unit")
    if (
        area is not None
        and card["area"]
        and number(card["area"]) != area
        or land is not None
        and card["land"]
        and _area(card["land"], land=True) != land
    ):
        raise RejectedCard("conflicting_house_measurement")


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
    description = item.get("description")
    if description is not None and not is_database_text(description):
        raise RejectedCard("invalid_description")
    room_count = params.get("Количество комнат")
    share = params.get("Условия продажи") == "Продажа доли"
    title = _text(item.get("title"))
    land = _area(params.get("Площадь участка"), land=True) if family == _HOUSE else None
    if family == _ROOM:
        rooms, area = _rooms_and_area(title, _text(description), area, share=share)
    elif family == _HOUSE:
        _check_house(title, area, land, share)
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
