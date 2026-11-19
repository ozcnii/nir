"""Map verified Domclick card data into P3 snapshots."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast
from urllib.parse import urljoin, urlsplit

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
from app.catalog.reference_defaults import REGIONS
from app.collection.collectors.domclick_state import NuxtState
from app.collection.collectors.nuxt_state import UNDEFINED
from app.collection.normalization import (
    RejectedCard,
    as_object,
    coordinate,
    decimal_2,
    number,
    optional_integer,
)

_REGIONS = {name.casefold(): ident for ident, name in REGIONS}
TYPE_IDS = {
    "flat": 1,
    "new_flat": 1,
    "room": 2,
    "house": 3,
    "house_part": 3,
    "townhouse": 3,
}


_STUDIO_TITLE = re.compile(
    r"(?:(?:студия|квартира-студия)(?:,? [0-9]+(?:[.,][0-9]+)? м[²2].*)?"
    r"|(?:купить|снять) (?:студию|квартиру-студию)(?:,.*| по адресу .*)?)"
)
_NUMBERED_FLAT_TITLE = re.compile(
    r"^(?:(?:купить|снять) )?"
    r"(?:[1-9]\d*-|одно|двух|тр[её]х|четыр[её]х|пяти)комнатн(?:ая|ую) квартир[ау]\b"
)
_DAILY_HOUSE_TITLE = re.compile(
    r"^(?:(?:снять )?(?:загородный|частный) дом|снять дом)\b"
)
_BLOCK_ADDRESS = re.compile(
    r"(?:^|, )\d+[а-яa-z]?, (?:блок|секция) (?:№ *)?\d+[а-яa-z]?$"
)
_COMPOUND_HOUSE_NUMBER = re.compile(r"(?:^|, )\d+[а-яa-z]?/\d+[а-яa-z]?$")
_DORMITORY = re.compile(r"\b(?:здание общежития|(?:статус|назначение) общежитие)\b")
_OFFERED_SHARE = re.compile(
    r"(?:^|[.\n]\s*)(?:прода[её]тся\s+)?(?:\d+/\d+\s+)?дол[яю]\b"
)
_ROOM_SIZE = re.compile(
    r"(?:^|[.!?\n]\s*)(?:прода[её]тся |сда[её]тся |уютная )?"
    r"комната(?: №?\s*[0-9]+)?(?: в квартире №?\s*[0-9]+)?"
    r"(?: площадью)?[ :]+([0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.\s*м)(?!\w)"
)
_ROOM_ADDRESS = re.compile(r"(?:^|, )комната (?:№\s*)?([0-9]+)$")
_ALLOCATED_ROOM = re.compile(
    r"^доля (?:соответствует|выделена в) комнат[еу] (?:№\s*)?([0-9]+)(?![0-9])"
)
_DUPLEX_AREA = re.compile(r"\bдуплекс (\d+(?:[.,]\d+)?) *м[²2]\b")


@dataclass(frozen=True)
class DailyListingQuote:
    card_url: str
    dates: str
    guests: str
    amount: str
    nightly: str


def _missing(value: object) -> bool:
    return value is None or value is UNDEFINED


def _optional(value: object) -> object:
    return None if value is UNDEFINED else value


def _record(value: object) -> dict[str, object]:
    if not _missing(value) and not isinstance(value, dict):
        raise RejectedCard("invalid_structure")
    return as_object(value)


def _text(value: object, *, problem: str = "invalid_text") -> str | None:
    if _missing(value):
        return None
    if not is_database_text(value):
        raise RejectedCard(problem)
    return value or None


def _point(lat: object, lon: object) -> tuple[Decimal | None, Decimal | None]:
    if _missing(lat) and _missing(lon):
        return None, None
    if _missing(lat) or _missing(lon):
        raise RejectedCard("invalid_coordinate_pair")
    return coordinate(lat, 90), coordinate(lon, 180)


def _area(value: object) -> Decimal | None:
    return (
        None
        if _missing(value)
        else decimal_2(value, minimum=Decimal("0.01"), maximum=Decimal("1e10"))
    )


def _known_price(
    value: object, kind: Literal["listed", "from"] = "listed"
) -> KnownPrice:
    return {
        "status": "known",
        "price_rub": decimal_2(value, minimum=Decimal(0), maximum=Decimal("1e14")),
        "price_kind": kind,
        "currency": "RUB",
    }


def _region(name: object) -> int:
    if isinstance(name, str):
        match = _REGIONS.get(" ".join(name.split()).casefold())
        if match is not None:
            return match
    raise RejectedCard("invalid_region")


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise RejectedCard("invalid_structure")
    return cast(list[object], value)


def _address(*parts: str | None) -> str | None:
    names: dict[str, str] = {}
    for part in parts:
        if part:
            for component in part.split(","):
                name = " ".join(component.split())
                if name:
                    names.setdefault(name.casefold(), name)
    return ", ".join(names.values()) or None


def _with_flat_number(address: str | None, flat: str | None) -> str | None:
    if flat and address:
        for segment in address.split(","):
            published = re.fullmatch(
                r"(?:квартира|кв\.?) *№? *(.+)", segment.strip(), re.IGNORECASE
            )
            if published and published[1].casefold() == flat.lstrip("№ ").casefold():
                return address
    return _address(address, f"кв. {flat}" if flat else None)


def _floors(floor: object, total: object) -> tuple[int | None, int | None]:
    level = optional_integer(_optional(floor), minimum=-32768)
    levels = optional_integer(_optional(total), minimum=1)
    if level is not None and levels is not None and level > levels:
        raise RejectedCard("floor_above_building")
    return level, levels


def _rooms(value: object, *unit_titles: str) -> int | None:
    rooms = optional_integer(_optional(value), minimum=0)
    if rooms == 0:
        titles = [" ".join(title.casefold().split()) for title in unit_titles]
        if any(_NUMBERED_FLAT_TITLE.match(title) for title in titles):
            raise RejectedCard("contradictory_rooms")
        if not any(_STUDIO_TITLE.fullmatch(title) for title in titles):
            raise RejectedCard("unconfirmed_studio")
    return rooms


def _land(data: dict[str, object]) -> Decimal | None:
    value = data.get("area")
    unit = data.get("areaUnit")
    if _missing(value):
        return None
    scale = {"сотка": 100, "м²": 1, "га": 10_000}
    if not isinstance(unit, str) or unit not in scale:
        raise RejectedCard("invalid_land_unit")
    return decimal_2(
        number(value) * scale[unit],
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12"),
    )


def _native_price(value: object) -> KnownPrice | Unavailable:
    record = as_object(value)
    if record.get("currency") == "RUB":
        try:
            return _known_price(record.get("price"))
        except RejectedCard:
            pass
    return {"status": "unavailable"}


def _photo_urls(values: list[object]) -> KnownPhotos | Unavailable:
    if not all(is_photo_url(value) for value in values):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], values)))}


def _native_address(data: dict[str, object]) -> tuple[int, str | None, bool]:
    parents = [_record(value) for value in _array(data.get("parents"))]
    subjects = [
        part.get("name")
        for part in parents
        if part.get("kind") == "province" and part.get("subkind") == "region"
    ]
    if len(subjects) != 1:
        raise RejectedCard("invalid_region")
    subject = _text(subjects[0], problem="invalid_region")
    localities = [
        _text(part.get("name"))
        for part in reversed(parents)
        if part.get("kind") == "locality"
    ]
    display = _text(data.get("displayName"), problem="invalid_address")
    if display:
        # The structured locality parents are ordered inward to outward.
        # Keep the display address (street, house, district) as published.
        display = ", ".join(
            name.strip() for name in display.split(",") if name.strip() != "Россия"
        )
    return _region(subject), _address(subject, *localities, display), any(localities)


def _native_room_area(
    rooms: int | None, area: Decimal | None, address: str | None, description: str
) -> Decimal | None:
    if _OFFERED_SHARE.search(description):
        published = _ROOM_ADDRESS.search((address or "").casefold())
        allocation = _ALLOCATED_ROOM.match(description)
        if (
            rooms != 1
            or not published
            or not allocation
            or published[1] != allocation[1]
        ):
            raise RejectedCard("unconfirmed_room_unit")
    if rooms == 1 and area is not None:
        measurements = {number(value) for value in _ROOM_SIZE.findall(description)}
        if len(measurements) == 1 and area not in measurements:
            raise RejectedCard("conflicting_room_area")
    # roomArea is a single-room field. The area of a multi-room offer is
    # unknown without a separately published area for the entire offer.
    return None if rooms is not None and rooms > 1 else area


def _check_native_house_unit(
    native_type: str,
    address: str | None,
    has_locality: bool,
    geocoded_name: str | None,
    description: str | None,
    area: Decimal | None,
) -> None:
    seller_text = " ".join((description or "").casefold().split())
    if _DORMITORY.search(seller_text):
        raise RejectedCard("unsupported_house_unit")
    if _OFFERED_SHARE.search(seller_text):
        raise RejectedCard("unallocated_house_share")
    if native_type == "house":
        return
    if area is not None and any(
        number(value) != area for value in _DUPLEX_AREA.findall(seller_text)
    ):
        raise RejectedCard("conflicting_house_area")
    published = (address or "").casefold()
    # A confirmed townhouse may use its own compound street address. A
    # number with a slash alone cannot establish a separately offered unit.
    street_address = geocoded_name.casefold() if geocoded_name else ""
    addressable = _BLOCK_ADDRESS.search(published) or (
        native_type == "townhouse"
        and bool(street_address)
        and street_address.partition(", ")[0] != street_address
        and published.endswith(", " + street_address)
        and _COMPOUND_HOUSE_NUMBER.search(street_address)
    )
    if not has_locality or not addressable:
        raise RejectedCard("unconfirmed_house_block")


def normalize_native_card(
    key: SnapshotKey,
    url: str,
    data: dict[str, object],
    *,
    native_type: str,
    deal: Deal,
    page_title: str,
) -> ActiveSnapshot:
    address = _record(data.get("address"))
    region_id, formatted, has_locality = _native_address(address)
    position = _record(address.get("position"))
    lat, lon = _point(position.get("lat"), position.get("lon"))
    info = _record(data.get("objectInfo"))
    house = _record(_record(data.get("house")).get("info"))
    property_type_id = TYPE_IDS[native_type]
    is_house = property_type_id == 3
    is_room = property_type_id == 2
    floor, floors = _floors(
        None if is_house else info.get("floor"), house.get("floors")
    )
    flat = _text(info.get("flatNumber")) if not is_house else None
    formatted = _with_flat_number(formatted, flat)
    area = _area(info.get("roomArea" if is_room else "area"))
    rooms = (
        optional_integer(_optional(data.get("roomsOffered")), minimum=1)
        if is_room
        else _rooms(info.get("rooms"), page_title if native_type == "flat" else "")
    )
    description = _text(info.get("description"), problem="invalid_description")
    if is_room:
        area = _native_room_area(rooms, area, formatted, (description or "").casefold())
    if is_house:
        _check_native_house_unit(
            native_type,
            formatted,
            has_locality,
            _text(address.get("name")),
            description,
            area,
        )
    photos: KnownPhotos | Unavailable = {"status": "unavailable"}
    if data.get("photos") == []:
        photos = {"status": "known", "urls": []}
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id,
        "property_type_id": property_type_id,
        "deal_type": deal,
        "market_segment": None,
        "address": formatted,
        "latitude": lat,
        "longitude": lon,
        "area_m2": area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": _land(_record(data.get("land"))) if is_house else None,
        "description": description,
        "price": _native_price(data.get("priceInfo")),
        "photos": photos,
    }


# The partner exposes a locality ID and title in the card, without a subject.
_PARTNER_REGIONS = {(366640, "Новокуйбышевск"): 68}


def _partner_properties(
    state: NuxtState,
    detail: dict[str, object],
    group_name: str,
    names: tuple[str, ...],
) -> dict[str, object]:
    result: dict[str, object] = {}
    count = 0
    for index in _array(state.field(detail, "propertyGroups")):
        group = _record(state.resolve(index))
        if state.field(group, "name") != group_name:
            continue
        count += 1
        for prop_index in _array(state.field(group, "properties")):
            prop = _record(state.resolve(prop_index))
            name = state.field(prop, "name")
            if not isinstance(name, str) or name not in names:
                continue
            if name in result:
                raise RejectedCard("duplicate_partner_property")
            if name == "area" and state.field(prop, "title") != "Площадь (м2)":
                raise RejectedCard("invalid_area_unit")
            result[name] = state.field(prop, "value")
    if count > 1 or (count == 0 and group_name == "basic_properties"):
        raise RejectedCard("invalid_partner_property_group")
    return result


def _partner_photos(
    state: NuxtState,
    detail: dict[str, object],
    url: str,
) -> KnownPhotos | Unavailable:
    try:
        media = state.field(detail, "media")
        images = _array(state.field(media, "images"))
        paths = [state.field(state.resolve(index), "path") for index in images]
    except (ValueError, RejectedCard):
        return {"status": "unavailable"}
    return _photo_urls(
        [
            urljoin(url, value)
            if isinstance(value, str) and value.startswith("//")
            else value
            for value in paths
        ]
    )


def normalize_partner_card(
    key: SnapshotKey,
    url: str,
    state: NuxtState,
    detail: dict[str, object],
    *,
    deal: Deal,
) -> ActiveSnapshot:
    try:
        kind = state.field(state.field(detail, "type"), "name")
        hotel = state.field(detail, "hotel")
        title = _text(state.field(detail, "title"))
        if not _missing(hotel) and (
            kind == "room" or title and re.search(r"\bномер\b", title.casefold())
        ):
            raise RejectedCard("hotel_or_ambiguous_room")
        if kind != "apartment":
            raise RejectedCard("unsupported_partner_type")
        location = _record(state.field(detail, "location"))
        city = _record(state.field(location, "city"))
        city_title = _text(state.field(city, "title"))
        city_id = state.field(city, "id")
        region_id = (
            _PARTNER_REGIONS.get((city_id, city_title))
            if type(city_id) is int and city_title is not None
            else None
        )
        if region_id is None:
            raise RejectedCard("invalid_region")
        lat, lon = _point(state.field(location, "lat"), state.field(location, "lng"))
        attributes = _partner_properties(
            state, detail, "basic_properties", ("rooms", "area", "floor", "max_floor")
        )
        descriptions = _partner_properties(
            state, detail, "detail_description", ("description",)
        )
        floor, floors = _floors(attributes.get("floor"), attributes.get("max_floor"))
        description = _text(
            descriptions.get("description"), problem="invalid_description"
        )
        address = _address(city_title, _text(state.field(location, "address")))
    except RejectedCard:
        raise
    except ValueError as error:
        raise RejectedCard("invalid_partner_reference") from error
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id,
        "property_type_id": TYPE_IDS["flat"],
        "deal_type": deal,
        "market_segment": None,
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "area_m2": _area(attributes.get("area")),
        "rooms": _rooms(attributes.get("rooms"), title or ""),
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": None,
        "description": description,
        # The 1,800-RUB amount lacks a verified tariff context on this partner.
        "price": {"status": "unavailable"},
        "photos": _partner_photos(state, detail, url),
    }


def _daily_price(
    quote: DailyListingQuote | None,
) -> KnownPrice | Unavailable:
    if (
        quote is not None
        and not quote.dates.strip()
        and " ".join(quote.guests.split()) == "1 гость"
    ):
        amount = re.fullmatch(
            r"(от )?([0-9][0-9 ]*(?:[.,][0-9]+)?) ₽", " ".join(quote.amount.split())
        )
        nightly = re.fullmatch(
            r"(от )?([0-9][0-9 ]*(?:[.,][0-9]+)?) ₽ за (?:1 )?сутки",
            " ".join(quote.nightly.split()),
        )
        if amount and nightly:
            try:
                price = _known_price(amount[2], "from" if amount[1] else "listed")
                if amount[1] == nightly[1] and price["price_rub"] == number(nightly[2]):
                    return price
            except RejectedCard:
                pass
    return {"status": "unavailable"}


def _gallery_photos(
    paths: list[object],
    gallery: list[str],
    directory: str,
) -> KnownPhotos | Unavailable:
    # Match by the published asset path with its extension omitted. The actual
    # src, including its transform path and query, comes from the main gallery.
    prefix = rf"/{directory}/[0-9a-f]{{2}}/[0-9a-f]{{2}}/[0-9a-f]+"
    pattern = re.compile(rf"({prefix})\.(?:jpg|webp|png)$")
    gallery_by_path: dict[str, str] = {}
    for url in gallery:
        if is_photo_url(url) and (match := pattern.search(urlsplit(url).path)):
            gallery_by_path.setdefault(match[1], url)
    urls: list[object] = []
    for path in paths:
        if not isinstance(path, str):
            return {"status": "unavailable"}
        match = pattern.fullmatch(path)
        urls.append(gallery_by_path.get(match[1]) if match else None)
    return _photo_urls(urls)


def normalize_daily_card(
    key: SnapshotKey,
    url: str,
    data: dict[str, object],
    *,
    deal: Deal,
    gallery: list[str],
    quote: DailyListingQuote | None,
    page_title: str,
) -> ActiveSnapshot:
    if data.get("category") != "flat":
        raise RejectedCard("unsupported_daily_type")
    heading = _text(data.get("title")) or ""
    if any(
        _DAILY_HOUSE_TITLE.match(" ".join(title.casefold().split()))
        for title in (heading, page_title)
    ):
        raise RejectedCard("contradictory_daily_type")
    if not _missing(data.get("area")) and data.get("areaUnit") != "м²":
        raise RejectedCard("invalid_area_unit")
    geo = _record(data.get("geoInfo"))
    address = _text(geo.get("address"), problem="invalid_address")
    region_id = _region(address.split(",", 1)[0] if address else None)
    lat, lon = _point(geo.get("lat"), geo.get("lon"))
    floor, floors = _floors(data.get("floor"), data.get("floorsMax"))
    photos = data.get("photos")
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id,
        "property_type_id": TYPE_IDS["flat"],
        "deal_type": deal,
        "market_segment": None,
        "address": _address(address),
        "latitude": lat,
        "longitude": lon,
        "area_m2": _area(data.get("area")),
        "rooms": _rooms(data.get("rooms"), heading, page_title),
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": None,
        "description": _text(data.get("description"), problem="invalid_description"),
        "price": _daily_price(quote),
        "photos": _gallery_photos(
            [as_object(photo).get("url") for photo in _array(photos)],
            gallery,
            "homestay",
        )
        if isinstance(photos, list)
        else {"status": "unavailable"},
    }
