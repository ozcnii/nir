"""Convert the supported Cian payload to catalog fields."""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import cast
from unicodedata import normalize

from app.catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPhotos,
    KnownPrice,
    Market,
    SnapshotKey,
    Unavailable,
    is_database_text,
    is_photo_url,
)
from app.catalog.reference_defaults import REGIONS

ROOM_TYPE_ID = 2
HOUSE_TYPE_ID = 3

_REGION_IDS = {name.casefold(): identifier for identifier, name in REGIONS}
_CIAN_REGION_LEVEL = 2
_CIAN_CITY_LEVEL = 1
# A city-only fallback is permitted for an independently verified place.
_CIAN_CITY_REGIONS = {(4966, "Самара"): "Самарская область"}
_M2_MAX = Decimal("1e10")
_LAND_M2_MAX = Decimal("1e12")
_PRICE_MAX = Decimal("1e14")


class RejectedCard(ValueError):
    """A recognized card has invalid required or structural data."""


# The integer part is either space-grouped in thousands or ungrouped.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)(?:[.,][0-9]+)?")


def number(value: object) -> Decimal:
    if isinstance(value, str):
        value = value.strip().replace("\u00a0", " ").replace("\u202f", " ")
        if not _DECIMAL_TEXT.fullmatch(value):
            raise RejectedCard("invalid_number")
        value = value.replace(" ", "").replace(",", ".")
    elif isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise RejectedCard("invalid_number")
    try:
        result = Decimal(value)
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


def address_component(value: object) -> str:
    if not is_database_text(value) or not value.strip():
        raise RejectedCard("invalid_address")
    name = " ".join(normalize("NFC", value).split())
    prefix, sep, rest = name.partition(" ")
    labels = {
        "ул.": "улица",
        "просп.": "проспект",
        "пер.": "переулок",
        "кв.": "квартира",
    }
    return f"{labels.get(prefix.casefold(), prefix)}{sep}{rest}"


def region_id(name: str) -> int:
    identifier = _REGION_IDS.get(name.casefold())
    if identifier is None:
        raise RejectedCard("invalid_region")
    return identifier


def coordinates(
    latitude: object, longitude: object
) -> tuple[Decimal | None, Decimal | None]:
    if latitude is None and longitude is None:
        return None, None
    if latitude is None or longitude is None:
        raise RejectedCard("invalid_coordinate_pair")
    return coordinate(latitude, 90), coordinate(longitude, 180)


def area_m2(value: object) -> Decimal | None:
    if value is None:
        return None
    return decimal_2(value, minimum=Decimal("0.01"), maximum=_M2_MAX)


def seller_description(value: object) -> str | None:
    if value is not None and not is_database_text(value):
        raise RejectedCard("invalid_description")
    return value or None


# Only a direct offer clause can establish the subject and a comparable area.
# Layout mentions, kitchen measurements and neighboring units have other scope.
_OFFER = r"(?:\A|(?<=[.!?;:\n]))\s*(?:прода[её]тся|продаются|сда[её]тся|сдаются|продаю|сдаю)\s+"
_AREA = (
    r"(?P<area>[0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.\s*м)"
    r"(?=[\s,.;!?]|$)"
)
_OFFERED_ROOM = re.compile(
    _OFFER + r"(?:одна\s+)?комнат[ау][:\s]+(?:площадью\s+)?" + _AREA,
    re.IGNORECASE,
)
_OTHER_UNIT = re.compile(r"\s+(?:в|из)\s+(?:соседн|друг)\w+", re.IGNORECASE)
_ROOM_SHARE = re.compile(
    _OFFER + r"(?:[0-9]+/[0-9]+\s+)?дол[яю]\s+(?:в\s+)?квартир[еы]\b",
    re.IGNORECASE,
)
_SPECIFIC_ROOM_SHARE = re.compile(
    _OFFER + r"(?:[0-9]+/[0-9]+\s+)?дол[яю]\s+комнаты\s+№?\s*[1-9][0-9]*\b",
    re.IGNORECASE,
)
_ALLOCATION = re.compile(
    r"\s*,\s*(?:выделена|предлагается)\s+(?:конкретная\s+)?комната\s+№\s*[1-9][0-9]*\b",
    re.IGNORECASE,
)

_HOUSE_SET_OR_PROJECT = re.compile(
    _OFFER + r"(?:(?:два|три|четыре|несколько|[2-9]|[1-9][0-9]+)\s+"
    r"(?:дом(?:а|ов)|коттедж(?:а|ей))|(?:типов(?:ой|ого)\s+)?проект\s+"
    r"(?:дома|коттеджа)|строительство\b|(?:земельный\s+)?участок\s+без\s+дома)\b",
    re.IGNORECASE,
)
_HOUSE_PART = re.compile(
    _OFFER + r"(?:(?:[0-9]+/[0-9]+\s+)?(?:дол[яю]|половин[ау]|часть)\s+"
    r"(?:в\s+)?(?:дом[ае]|коттедж[ае])|(?:жилой\s+)?блок|секция|таунхаус)\b",
    re.IGNORECASE,
)
_ADDRESSABLE_BLOCK = re.compile(
    r"(?:блок|секция|таунхаус)\s+(?:№\s*)?[0-9][0-9a-я/.-]*\b",
    re.IGNORECASE,
)
_OFFERED_HOUSE_AREA = re.compile(
    _OFFER + r"(?:дом|коттедж)[,\s]+(?:(?:общей\s+)?площадью\s+)?" + _AREA,
    re.IGNORECASE,
)


def check_room(
    rooms: int | None, area: Decimal | None, description: str | None
) -> tuple[int | None, Decimal | None]:
    text = description or ""
    scoped_share = _SPECIFIC_ROOM_SHARE.search(text) is not None
    for share in _ROOM_SHARE.finditer(text):
        if not _ALLOCATION.match(text, share.end()):
            raise RejectedCard("unconfirmed_room_unit")
        scoped_share = True
    measured = {
        area_m2(match["area"])
        for match in _OFFERED_ROOM.finditer(text)
        if not _OTHER_UNIT.match(text, match.end())
    }
    if not measured and not scoped_share:
        return rooms, area
    if rooms not in (None, 1):
        raise RejectedCard("conflicting_room_count")
    if area is not None:
        measured.add(area)
    if len(measured) > 1:
        raise RejectedCard("conflicting_room_area")
    return 1, next(iter(measured), None)


def _check_house(
    area: Decimal | None,
    building: dict[str, object],
    names: list[str],
    description: str | None,
) -> None:
    text = description or ""
    scoped_block = any(_ADDRESSABLE_BLOCK.fullmatch(part) for part in names)
    if _HOUSE_SET_OR_PROJECT.search(text) or (
        _HOUSE_PART.search(text) and not scoped_block
    ):
        raise RejectedCard("unconfirmed_house_unit")
    # A block can be described together with its enclosing building. Compare the
    # top-level area and building area only for ordinary full-house offers.
    if scoped_block:
        return
    areas = {
        area_m2(match["area"])
        for match in _OFFERED_HOUSE_AREA.finditer(text)
        if not _OTHER_UNIT.match(text, match.end())
    }
    areas.update((area, area_m2(building.get("totalArea"))))
    if len(areas - {None}) > 1:
        raise RejectedCard("conflicting_house_area")


def check_floor(floor: int | None, floors: int | None) -> None:
    if floor is not None and floors is not None and floor > floors:
        raise RejectedCard("floor_above_building")


def _region(components: list[dict[str, object]]) -> str:
    locations = [part for part in components if part.get("type") == "location"]
    subjects = [
        part for part in locations if part.get("locationTypeId") == _CIAN_REGION_LEVEL
    ]
    if len(subjects) == 1:
        return address_component(subjects[0].get("fullName"))
    if not subjects:
        for place in locations:
            identifier = place.get("id")
            name = place.get("fullName")
            if (
                place.get("locationTypeId") == _CIAN_CITY_LEVEL
                and type(identifier) is int
                and isinstance(name, str)
                and (region := _CIAN_CITY_REGIONS.get((identifier, name)))
            ):
                return region
    raise RejectedCard("invalid_region")


def _land_area(offer: dict[str, object]) -> Decimal | None:
    land = _optional_object(offer.get("land"))
    amount = land.get("area")
    if amount is None:
        return None
    if land.get("areaUnitType") != "sotka":
        raise RejectedCard("invalid_land_unit")
    value = number(amount)
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    square_metres = Decimal((sign, digits, exponent + 2))
    return decimal_2(square_metres, minimum=Decimal("0.01"), maximum=_LAND_M2_MAX)


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
    if not isinstance(display, str):
        return {"status": "unavailable"}
    label = " ".join(display.casefold().split())
    unit = {"sale": "₽", "month_rent": "₽/мес.", "day_rent": "₽/сут."}[deal]
    if not label.endswith(unit):
        return {"status": "unavailable"}
    period = bargain.get("paymentPeriod")
    if period is not None and (deal != "month_rent" or period != "monthly"):
        return {"status": "unavailable"}
    if deal == "day_rent" and bargain.get("leaseTermType") in ("longTerm", "fewMonths"):
        return {"status": "unavailable"}
    try:
        price = decimal_2(
            product_offer.get("price"), minimum=Decimal(0), maximum=_PRICE_MAX
        )
        stated = bargain.get("price")
        if (
            stated is not None
            and decimal_2(stated, minimum=Decimal(0), maximum=_PRICE_MAX) != price
        ):
            return {"status": "unavailable"}
        displayed_amount = label.removeprefix("от ").removesuffix(unit).strip()
        if number(displayed_amount) != price:
            return {"status": "unavailable"}
    except RejectedCard:
        return {"status": "unavailable"}
    return {
        "status": "known",
        "price_rub": price,
        "price_kind": "from" if label.startswith("от ") else "listed",
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
        or not all(is_photo_url(url) for url in cast(list[object], images))
    ):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], images)))}


def normalize_cian_card(
    url: str,
    key: SnapshotKey,
    data: dict[str, object],
    product: dict[str, object],
    *,
    deal: Deal,
    property_type_id: int,
    market_segment: Market | None,
) -> ActiveSnapshot:
    offer = as_object(data.get("offer"))
    geo = _optional_object(offer.get("geo"))
    address = geo.get("address")
    if not isinstance(address, list):
        raise RejectedCard("invalid_region")
    components = [as_object(item) for item in cast(list[object], address)]
    subject = _region(components)
    names = [address_component(part.get("fullName")) for part in components]
    if subject not in names:
        names.insert(0, subject)

    point = _optional_object(geo.get("coordinates"))
    latitude, longitude = coordinates(point.get("lat"), point.get("lng"))
    house = property_type_id == HOUSE_TYPE_ID
    room = property_type_id == ROOM_TYPE_ID
    floor = (
        None if house else optional_integer(offer.get("floorNumber"), minimum=-32768)
    )
    building = _optional_object(offer.get("building"))
    floors = optional_integer(building.get("floorsCount"), minimum=1)
    check_floor(floor, floors)
    rooms = optional_integer(
        offer.get("roomsForSaleCount" if room else "roomsCount"),
        minimum=1 if house or room else 0,
    )
    if rooms == 0:
        raise RejectedCard("unconfirmed_studio")
    area = area_m2(offer.get("roomArea" if room else "totalArea"))
    description = seller_description(offer.get("description"))
    if room:
        rooms, area = check_room(rooms, area, description)
    if house:
        _check_house(area, building, names, description)

    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region_id(subject),
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
