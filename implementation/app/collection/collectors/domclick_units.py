"""Check explicit claims about the unit offered in a native Domclick card."""

import re
from decimal import Decimal

from app.collection.normalization import RejectedCard, number, optional_integer

_CLAUSE = r"(?:\A|(?<=[.!?;:\n]))\s*"
_DEAL = r"(?:прода[её]тся|продаются|сда[её]тся|сдаются|продаю|сдаю|продам)"
_OFFER = _CLAUSE + _DEAL + r"\s+"
_SQUARE_METRES = r"(?P<area>[0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.\s*м)(?!\w)"
_ROOM_SIZE = re.compile(
    _CLAUSE + rf"(?:{_DEAL}\s+)?(?:одна |одну |уютная )?комнат[ау]"
    r"(?: №?\s*[0-9]+)?(?: в квартире №?\s*[0-9]+)?"
    r"(?: площадью)?[ :]+" + _SQUARE_METRES,
    re.IGNORECASE,
)
_NON_OFFERED_UNIT = re.compile(r"\s+(?:в|из)\s+(?:соседн|друг)\w*", re.IGNORECASE)
_COUNTS = {"одна": 1, "одну": 1, "две": 2, "три": 3, "четыре": 4}
_COUNTED_ROOMS = re.compile(
    _OFFER + r"([0-9]{1,5}|одна|одну|две|три|четыре|несколько)\s+комнат(?:а|ы|у)?\b",
    re.IGNORECASE,
)
_OFFERED_SHARE = re.compile(
    _OFFER + r"(?:не\s*выделенн(?:ая|ую)\s+)?(?:[0-9]+/[0-9]+\s+)?дол[яю]\b"
    r"|\A\s*доля(?=\s*:|\s+(?:соответствует|выделена))",
    re.IGNORECASE,
)
_ROOM_ADDRESS = re.compile(r"(?:^|, )комната (?:№\s*)?([0-9]+)$", re.IGNORECASE)
_ALLOCATED_ROOM = re.compile(
    r"\s+(?:соответствует|выделена в) комнат[еу] (?:№\s*)?([0-9]+)(?![0-9])",
    re.IGNORECASE,
)
_INDEPENDENT_BLOCK = re.compile(
    r"(?:^|, )\d+[а-яa-z]?, (?:блок|секция) (?:№ *)?\d+[а-яa-z]?$"
)
_COMPOUND_HOUSE_NUMBER = re.compile(r"(?:^|, )\d+[а-яa-z]?/\d+[а-яa-z]?$")
_HOUSE_SET_OR_PROJECT = re.compile(
    _OFFER + r"(?:(?:два|три|четыре|несколько|[2-9]|[1-9][0-9]+)\s+"
    r"(?:дом(?:а|ов)|коттедж(?:а|ей))|"
    r"(?:типов(?:ой|ого)\s+)?проект\s+(?:дома|коттеджа)|строительство\b|"
    r"(?:земельный\s+)?участок\s+без\s+дома|только (?:земельный )?участок)\b",
    re.IGNORECASE,
)
_OFFERED_HOUSE_PART = re.compile(
    _OFFER + r"(?:половин[ау]|часть)\s+(?:дом[ае]|коттедж[ае])\b",
    re.IGNORECASE,
)
_DORMITORY = re.compile(
    r"\b(?:здание общежития|(?:статус|назначение) общежитие)\b", re.IGNORECASE
)
_HOUSE_SIZE = re.compile(
    rf"(?:{_OFFER}(?:дом|коттедж|дуплекс)|{_CLAUSE}(?:(?:одно|двух)этажный )?дуплекс)"
    r"[, :]+(?:(?:общей )?площадью\s+)?" + _SQUARE_METRES,
    re.IGNORECASE,
)


def _published_areas(pattern: re.Pattern[str], text: str) -> set[Decimal]:
    return {
        number(match["area"])
        for match in pattern.finditer(text)
        if not _NON_OFFERED_UNIT.match(text, match.end())
    }


def check_room(
    rooms: int | None,
    area: Decimal | None,
    address: str | None,
    description: str | None,
) -> tuple[int | None, Decimal | None]:
    text = description or ""
    counts = set() if rooms is None else {rooms}
    multiple = False
    for match in _COUNTED_ROOMS.finditer(text):
        count = match[1].casefold()
        if _NON_OFFERED_UNIT.match(text, match.end()):
            continue
        if count == "несколько":
            multiple = True
        elif count in _COUNTS:
            counts.add(_COUNTS[count])
        elif (published_count := optional_integer(count, minimum=1)) is not None:
            counts.add(published_count)
    for share in _OFFERED_SHARE.finditer(text):
        published = _ROOM_ADDRESS.search(address or "")
        allocation = _ALLOCATED_ROOM.match(text, share.end())
        if not published or not allocation or published[1] != allocation[1]:
            raise RejectedCard("unconfirmed_room_unit")
        counts.add(1)
    if len(counts) > 1 or (multiple and counts == {1}) or 0 in counts:
        raise RejectedCard("conflicting_room_count")
    detected = next(iter(counts), None)
    if multiple or (detected is not None and detected > 1):
        # roomArea measures one room, never the sum for a set.
        return detected, None
    areas = _published_areas(_ROOM_SIZE, text)
    if areas and area is not None:
        areas.add(area)
    if len(areas) > 1:
        raise RejectedCard("conflicting_room_area")
    return detected, area


def check_house(
    native_type: str,
    address: str | None,
    has_locality: bool,
    geocoded_name: str | None,
    description: str | None,
    area: Decimal | None,
) -> None:
    text = description or ""
    if _DORMITORY.search(text) or _HOUSE_SET_OR_PROJECT.search(text):
        raise RejectedCard("unsupported_house_unit")
    if _OFFERED_SHARE.search(text):
        raise RejectedCard("unallocated_house_share")
    published = (address or "").casefold()
    block = _INDEPENDENT_BLOCK.search(published)
    if _OFFERED_HOUSE_PART.search(text) and not block:
        raise RejectedCard("unconfirmed_house_block")
    if area is not None and _published_areas(_HOUSE_SIZE, text) - {area}:
        raise RejectedCard("conflicting_house_area")
    if native_type == "house":
        return

    # Require the structured street address alongside a compound number.
    street_address = geocoded_name.casefold() if geocoded_name else ""
    townhouse = (
        native_type == "townhouse"
        and bool(street_address)
        and street_address.partition(", ")[0] != street_address
        and published.endswith(", " + street_address)
        and _COMPOUND_HOUSE_NUMBER.search(street_address)
    )
    if not has_locality or not (block or townhouse):
        raise RejectedCard("unconfirmed_house_block")
