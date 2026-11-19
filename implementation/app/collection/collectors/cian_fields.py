"""Convert the supported Cian payload to catalog fields."""

import re
from collections.abc import Iterator
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
# Observed relationships in native Cian address states and the oblast catalog.
_CIAN_SUBJECTS = {4608: "Самарская область", 4609: "Саратовская область"}
_CIAN_CITIES = {
    4966: ("Самара", 4608),
    4969: ("Саратов", 4609),
    5066: ("Энгельс", 4609),
}
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
_OFFER = r"(?:\A|(?<=[.!?;:,\n]))\s*(?:прода[её]тся|продаются|сда[её]тся|сдаются|продаю|сдаю)\s+"
_AREA = (
    r"(?P<area>[0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.\s*м)"
    r"(?=[\s,.;!?]|$)"
)
_ROOM_COUNTS = {"одна": 1, "одну": 1, "две": 2, "три": 3, "четыре": 4}
_OFFERED_ROOMS = re.compile(
    _OFFER
    + rf"(?:(?P<count>[1-9][0-9]{{0,4}}|{'|'.join(_ROOM_COUNTS)}|несколько)\s+)?"
    + r"(?P<unit>комнат[ауы]?)\b",
    re.IGNORECASE,
)
_ROOM_NUMBER = re.compile(r"\s*№\s*([1-9][0-9]*)")
_LIST_LINK = r"(?:\s*,\s*(?:и\s+)?|\s+и\s+)"
_NEXT_NUMBERED_ROOM = re.compile(_LIST_LINK + r"(?:комнат[ауы]?\s+)?", re.IGNORECASE)
_JOINT_LOT = re.compile(r"[\s,]+одним\s+лотом\b", re.IGNORECASE)
_OWN_AREA = re.compile(r"[,\s:]+(?:(?:общей\s+)?площадью\s+)?" + _AREA, re.IGNORECASE)
_SET_AREA = re.compile(
    r"[,\s:]+(?:общей|суммарной)\s+площадью\s+" + _AREA, re.IGNORECASE
)
_NEXT_CLAIM = r"(?:[.!?;]\s*|\n\s*)"
_CLAIM_VALUE = r"\s*(?:[—–:-]\s*)?"
_LINKED_SET_AREA = re.compile(
    _NEXT_CLAIM
    + r"(?:общая|суммарная)\s+площадь\s+(?:этих|предлагаемых)\s+комнат\b"
    + _CLAIM_VALUE
    + _AREA,
    re.IGNORECASE,
)
_OTHER_UNIT = re.compile(r"[\s,]+(?:в|из)\s+(?:соседн|друг)\w+", re.IGNORECASE)
_SEPARATE_OFFER = re.compile(
    r"[\s,]+(?:отдельно|по\s+отдельности|не\s+одним\s+лотом)\b",
    re.IGNORECASE,
)
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

_UNSUPPORTED_HOUSE_SUBJECT = (
    r"(?:(?:два|три|четыре|несколько|[2-9]|[1-9][0-9]+)\s+"
    r"(?:дом(?:а|ов)|коттедж(?:а|ей))|(?:типов(?:ой|ого)\s+)?проект\s+"
    r"(?:дома|коттеджа)|строительство|(?:земельный\s+)?участок\s+без\s+дома)"
)
_BLOCK_KINDS = r"блок|секци[яю]|таунхаус"
_HOUSE_SUBJECT = r"(?:дом|коттедж)"
_WHOLE_QUALIFIER = r"(?:весь|целый)"
_PART_SUBJECT = (
    rf"(?:(?:{_WHOLE_QUALIFIER}\s+)?{_HOUSE_SUBJECT}\s*,\s*точнее\s+)?"
    r"(?:только\s+)?(?:(?:[0-9]+/[0-9]+\s+)?(?:дол[яю]|половин[ау]|часть)\s+"
    rf"(?:в\s+)?(?:дом[ае]|коттедж[ае])|(?:жилой\s+)?(?P<block>{_BLOCK_KINDS}))"
)
_HOUSE_OFFER = re.compile(
    _OFFER
    + rf"(?:(?P<unsupported>{_UNSUPPORTED_HOUSE_SUBJECT})|(?P<part>{_PART_SUBJECT})"
    + rf"|(?:(?P<whole>{_WHOLE_QUALIFIER})\s+)?"
    + _HOUSE_SUBJECT
    + r")\b",
    re.IGNORECASE,
)
_BLOCK_LABEL = rf"(?P<kind>{_BLOCK_KINDS})\s+(?:№\s*)?(?P<number>[0-9][0-9a-я/.-]*)\b"
_ADDRESSABLE_BLOCK = re.compile(_BLOCK_LABEL, re.IGNORECASE)
_ADDRESS_BLOCK_SCOPE = re.compile(
    rf"(?:\A\s*|\bдом\s+(?:№\s*)?[0-9][0-9a-я/.-]*\s+)(?=(?:{_BLOCK_KINDS})\b)",
    re.IGNORECASE,
)
_NEXT_HOUSE = re.compile(
    _LIST_LINK + rf"(?:(?:второй|ещ[её] один)\s+)?{_HOUSE_SUBJECT}\b", re.IGNORECASE
)
_PLOT_MEASUREMENT = (
    r"(?P<land>[0-9]+(?:[.,][0-9]+)?)\s*"
    r"(?P<unit>сот(?:ок|ки|ка)?|м[²2]|кв\.\s*м)(?=[\s,.;!?]|$)"
)
_LAND_IN_OFFER = re.compile(r"[\s,]+на\s+участке\s+" + _PLOT_MEASUREMENT, re.IGNORECASE)
_LINKED_PLOT_AREA = re.compile(
    _NEXT_CLAIM
    + r"площадь\s+предлагаемого\s+участка\b"
    + _CLAIM_VALUE
    + _PLOT_MEASUREMENT,
    re.IGNORECASE,
)


def _linked_claims(
    text: str, start: int, pattern: re.Pattern[str]
) -> Iterator[re.Match[str]]:
    while (claim := pattern.match(text, start)) and not _OTHER_UNIT.match(
        text, claim.end()
    ):
        yield claim
        start = claim.end()


def _numbered_rooms(
    text: str, start: int
) -> tuple[set[str], re.Match[str] | None, int]:
    numbers: set[str] = set()
    first_area = None
    member = _ROOM_NUMBER.match(text, start)
    while member:
        end = member.end()
        area = None if _SET_AREA.match(text, end) else _OWN_AREA.match(text, end)
        if area:
            end = area.end()
        if numbers and (
            _OTHER_UNIT.match(text, end) or _SEPARATE_OFFER.match(text, end)
        ):
            break
        if not numbers:
            first_area = area
        numbers.add(member[1])
        start = end
        link = _NEXT_NUMBERED_ROOM.match(text, end)
        member = _ROOM_NUMBER.match(text, link.end()) if link else None
    return numbers, first_area, start


def check_room(
    rooms: int | None, area: Decimal | None, description: str | None
) -> tuple[int | None, Decimal | None]:
    text = description or ""
    counts = {rooms} if rooms is not None else set()
    if _SPECIFIC_ROOM_SHARE.search(text):
        counts.add(1)
    for share in _ROOM_SHARE.finditer(text):
        if not _ALLOCATION.match(text, share.end()):
            raise RejectedCard("unconfirmed_room_unit")
        counts.add(1)

    single_areas: set[Decimal] = set()
    set_areas: set[Decimal] = set()
    multiple = False
    for offer in _OFFERED_ROOMS.finditer(text):
        raw_count = (offer["count"] or "").casefold()
        singular = offer["unit"].casefold() in ("комната", "комнату")
        bundle = not singular or raw_count not in ("", "1", "одна", "одну")
        room_numbers, first_area, end = _numbered_rooms(text, offer.end())
        bundle = bundle or len(room_numbers) > 1
        if bundle and (lot := _JOINT_LOT.match(text, end)):
            end = lot.end()
        measurement = (
            _SET_AREA.match(text, end)
            if bundle
            else first_area or _OWN_AREA.match(text, end)
        )
        if _OTHER_UNIT.match(text, end) or (
            measurement and _OTHER_UNIT.match(text, measurement.end())
        ):
            continue

        if raw_count.isdecimal():
            count = optional_integer(raw_count, minimum=1)
            if count is not None:
                counts.add(count)
        elif raw_count in _ROOM_COUNTS:
            counts.add(_ROOM_COUNTS[raw_count])
        elif not bundle:
            counts.add(1)
        else:
            multiple = True
        if len(room_numbers) > 1:
            counts.add(len(room_numbers))
        if measurement and (measured := area_m2(measurement["area"])):
            (set_areas if bundle else single_areas).add(measured)
        if bundle:
            after_offer = measurement.end() if measurement else end
            for claim in _linked_claims(text, after_offer, _LINKED_SET_AREA):
                if measured := area_m2(claim["area"]):
                    set_areas.add(measured)

    if len(counts) > 1 or (multiple and 1 in counts):
        raise RejectedCard("conflicting_room_count")
    offered_count = next(iter(counts), None)
    if multiple or (offered_count is not None and offered_count > 1):
        # The singular roomArea source field has no proven set scope.
        if len(set_areas) > 1:
            raise RejectedCard("conflicting_room_area")
        return offered_count, next(iter(set_areas), None)
    if area is not None:
        single_areas.add(area)
    if len(single_areas) > 1:
        raise RejectedCard("conflicting_room_area")
    return offered_count, next(iter(single_areas), None)


def _block(match: re.Match[str]) -> tuple[str, str]:
    kind = match["kind"].casefold()
    return ("секция" if kind == "секцию" else kind, match["number"].casefold())


def _house_measurements(
    text: str, start: int
) -> tuple[re.Match[str] | None, re.Match[str] | None, int]:
    area = _OWN_AREA.match(text, start)
    if area:
        start = area.end()
    plot = _LAND_IN_OFFER.match(text, start)
    return area, plot, plot.end() if plot else start


def _check_house(
    area: Decimal | None,
    plot: Decimal | None,
    building: dict[str, object],
    names: list[str],
    description: str | None,
) -> None:
    text = description or ""
    blocks = set()
    for part in ", ".join(names).split(","):
        if scope := _ADDRESS_BLOCK_SCOPE.search(part):
            block = _ADDRESSABLE_BLOCK.fullmatch(part, scope.end())
            if block is None:
                raise RejectedCard("unconfirmed_house_unit")
            blocks.add(_block(block))
    if len(blocks) > 1:
        raise RejectedCard("conflicting_house_unit")
    areas = {area}
    plots = {plot}

    def record(
        measured: re.Match[str] | None, lot: re.Match[str] | None, end: int
    ) -> None:
        if measured:
            areas.add(area_m2(measured["area"]))
        for claim in (lot, *_linked_claims(text, end, _LINKED_PLOT_AREA)):
            if claim:
                plots.add(
                    _plot_area(
                        claim["land"], sotka=claim["unit"].casefold().startswith("сот")
                    )
                )

    if not blocks:
        areas.add(area_m2(building.get("totalArea")))

    for offer in _HOUSE_OFFER.finditer(text):
        block_start = offer.start("block")
        block = (
            _ADDRESSABLE_BLOCK.match(text, block_start) if block_start >= 0 else None
        )
        measured, lot, end = _house_measurements(
            text, block.end() if block else offer.end()
        )
        if _OTHER_UNIT.match(text, end):
            continue
        if offer["unsupported"]:
            raise RejectedCard("unconfirmed_house_unit")
        if offer["part"]:
            if not blocks:
                raise RejectedCard("unconfirmed_house_unit")
            if block and _block(block) not in blocks:
                raise RejectedCard("conflicting_house_unit")
            record(measured, lot, end)
            continue
        if blocks and offer["whole"]:
            raise RejectedCard("conflicting_house_unit")
        if second := _NEXT_HOUSE.match(text, end):
            _, _, second_end = _house_measurements(text, second.end())
            if _JOINT_LOT.match(text, second_end):
                raise RejectedCard("unconfirmed_house_unit")
        # When a block is offered, enclosing house measurements are another scope.
        if not blocks:
            record(measured, lot, end)

    if len(areas - {None}) > 1:
        raise RejectedCard("conflicting_house_area")
    if len(plots - {None}) > 1:
        raise RejectedCard("conflicting_land_area")


def check_floor(floor: int | None, floors: int | None) -> None:
    if floor is not None and floors is not None and floor > floors:
        raise RejectedCard("floor_above_building")


def _region(components: list[dict[str, object]]) -> str:
    subjects = []
    city_subjects = set()
    for part in components:
        if part.get("type") != "location":
            continue
        kind = part.get("locationTypeId")
        identifier = part.get("id")
        if kind == _CIAN_REGION_LEVEL:
            name = address_component(part.get("fullName"))
            if isinstance(identifier, int) and (
                expected := _CIAN_SUBJECTS.get(identifier)
            ):
                if expected.casefold() != name.casefold():
                    raise RejectedCard("conflicting_region")
            subjects.append(name)
        if (
            kind == _CIAN_CITY_LEVEL
            and isinstance(identifier, int)
            and (known := _CIAN_CITIES.get(identifier))
        ):
            city, subject_id = known
            if address_component(part.get("fullName")).casefold() != city.casefold():
                raise RejectedCard("conflicting_region")
            city_subjects.add(_CIAN_SUBJECTS[subject_id])
    if len(subjects) > 1 or len(city_subjects) > 1:
        raise RejectedCard("invalid_region")
    city_subject = next(iter(city_subjects), None)
    if subjects:
        subject = subjects[0]
        if city_subject and subject.casefold() != city_subject.casefold():
            raise RejectedCard("conflicting_region")
        return subject
    if city_subject:
        return city_subject
    raise RejectedCard("invalid_region")


def _plot_area(amount: object, *, sotka: bool) -> Decimal:
    sign, digits, exponent = number(amount).as_tuple()
    assert isinstance(exponent, int)
    square_metres = Decimal((sign, digits, exponent + 2 * int(sotka)))
    return decimal_2(square_metres, minimum=Decimal("0.01"), maximum=_LAND_M2_MAX)


def _land_area(offer: dict[str, object]) -> Decimal | None:
    land = _optional_object(offer.get("land"))
    amount = land.get("area")
    if amount is None:
        return None
    if land.get("areaUnitType") != "sotka":
        raise RejectedCard("invalid_land_unit")
    return _plot_area(amount, sotka=True)


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
    land_area = _land_area(offer) if house else None
    if house:
        _check_house(area, land_area, building, names, description)

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
        "land_area_m2": land_area,
        "description": description,
        "price": _price(product, data, offer, deal),
        "photos": _photos(product, offer),
    }
