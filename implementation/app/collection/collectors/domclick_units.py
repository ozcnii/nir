"""Check explicit claims about the unit offered in a native Domclick card."""

import re
from decimal import Decimal

from app.collection.normalization import (
    RejectedCard,
    decimal_2,
    number,
    optional_integer,
)

_CLAUSE = r"(?:\A|(?<=[.!?;:\n]))\s*"
_DEAL = r"(?:прода[её]тся|продаются|сда[её]тся|сдаются|продаю|сдаю|продам)"
_OFFER = _CLAUSE + _DEAL + r"\s+"
_AMOUNT = r"[0-9]+(?:[.,][0-9]+)?"
_SQUARE_METRE_UNIT = r"(?:м[²2]|кв\.\s*м)(?!\w)"
_SQUARE_METRES = rf"(?P<area>{_AMOUNT})\s*{_SQUARE_METRE_UNIT}"
_ROOM_SIZE = re.compile(
    _CLAUSE + rf"(?:{_DEAL}\s+)?(?:одна |одну |уютная )?комнат[ау]"
    r"(?: №?\s*[0-9]+)?(?: в квартире №?\s*[0-9]+)?"
    r"(?: площадью)?[ :]+" + _SQUARE_METRES,
    re.IGNORECASE,
)
_NON_OFFERED_UNIT = re.compile(
    r"\s*(?:,\s*)?(?:(?:в|из|у)\s+(?:сосед|друг)\w*|отдельно|не входит)\b",
    re.IGNORECASE,
)
_COUNTS = {"одна": 1, "одну": 1, "две": 2, "три": 3, "четыре": 4}
_COUNTED_ROOMS = re.compile(
    _OFFER + r"([0-9]{1,5}|одна|одну|две|три|четыре|несколько)\s+комнат(?:а|ы|у)?\b",
    re.IGNORECASE,
)
_ROOM_ID = r"№\s*[1-9][0-9]{0,4}"
_ROOM_LIST = re.compile(
    _OFFER + r"(?:(?:единый\s+)?лот\s+из\s+)?комнат[ауые]?:?\s*"
    rf"(?P<rooms>{_ROOM_ID}(?:\s*(?:,\s*(?:и\s+)?|\s+и\s+)"
    rf"(?:комнат[ауые]\s+)?{_ROOM_ID})+)",
    re.IGNORECASE,
)
_ROOM_SET_AREA = re.compile(
    r"\s*,?\s*(?:одним лотом(?:,\s*|\s+))?"
    r"(?:(?:общей|суммарной) площадью|(?:общая|суммарная) площадь)"
    r"\s*[:—-]?\s*" + _SQUARE_METRES,
    re.IGNORECASE,
)
_ROOM_SET_AREA_CLAUSE = re.compile(
    _CLAUSE + r"(?:общая|суммарная) площадь "
    r"(?:набора комнат|(?:(?:этих|предлагаемых|продаваемых|сдаваемых)\s+)?комнат)"
    r"\s*[:—-]?\s*" + _SQUARE_METRES,
    re.IGNORECASE,
)
_UNALLOCATED = r"не(?:\s*выделенн(?:ая|ую)|делим(?:ая|ую))"
_OFFERED_SHARE = re.compile(
    _OFFER + rf"(?:{_UNALLOCATED}\s+)?(?:[0-9]+/[0-9]+\s+)?дол[яю]\b"
    rf"|\A\s*{_UNALLOCATED}\s+доля\b"
    r"|\A\s*доля(?=\s*:|\s+(?:соответствует|выделена|не (?:выделена|определена)))",
    re.IGNORECASE,
)
_ROOM_ADDRESS = re.compile(r"(?:^|, )комната (?:№\s*)?([0-9]+)$", re.IGNORECASE)
_ALLOCATED_ROOM = re.compile(
    r"\s+(?:соответствует|выделена в) комнат[еу] (?:№\s*)?([0-9]+)(?![0-9])",
    re.IGNORECASE,
)
_ALLOCATION_DENIAL = r"\b(?:доля|комната)\s+не\s+(?:выделена|определена)\b"
_SEPARATE_DENIAL = re.compile(_CLAUSE + _ALLOCATION_DENIAL, re.IGNORECASE)
_CONTRADICTORY_ALLOCATION = re.compile(
    _ALLOCATION_DENIAL + rf"|\s+(?:и|или)\s+комнат[еу]\s+{_ROOM_ID}",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[.!?;](?=\s|$)|\n")
_BUILDING_NUMBER = r"\d+[а-яa-z]?(?:[/.-]\d+[а-яa-z]?)*"
_BLOCK_SCOPE = r"блок|секци[яю]"
_BLOCK_MARKER = re.compile(
    rf"(?:^|, |(?=\bдом\s+))(?:дом\s+)?(?:№\s*)?(?:{_BUILDING_NUMBER}\s+)?"
    rf"(?:{_BLOCK_SCOPE})\b",
)
_EXCLUSIVE_OFFER = r"(?:(?:только|лишь)\s+)?"
_INDEPENDENT_BLOCK = re.compile(
    rf"(?:^|, )(?:дом )?{_BUILDING_NUMBER}, (?P<scope>{_BLOCK_SCOPE}) "
    rf"(?:№\s*)?(?P<identifier>{_BUILDING_NUMBER})$",
    re.IGNORECASE,
)
_HOUSE_ADDRESS = re.compile(
    rf"(?:^|, )(?:дом )?(?P<identifier>{_BUILDING_NUMBER})"
    rf"(?=, (?:{_BLOCK_SCOPE}|участок)\b|$)",
    re.IGNORECASE,
)
_PLOT_ADDRESS = re.compile(
    rf"(?:^|, )участок (?:№\s*)?(?P<identifier>{_BUILDING_NUMBER})(?:,|$)",
    re.IGNORECASE,
)
_OFFERED_ADDRESS = re.compile(
    _OFFER + _EXCLUSIVE_OFFER + rf"(?P<scope>дом|коттедж|участок|{_BLOCK_SCOPE})"
    rf"\s+№\s*(?P<identifier>{_BUILDING_NUMBER})(?![\w/])",
    re.IGNORECASE,
)
_BLOCK_SIZE = re.compile(
    rf"(?:{_OFFER}(?:{_BLOCK_SCOPE})(?: №?\s*{_BUILDING_NUMBER})?[, :]+"
    r"(?:(?:общей )?площадью\s+)?|"
    rf"{_CLAUSE}(?:общая )?площадь (?:блока|секции)\s*[:—-]?\s*)" + _SQUARE_METRES,
    re.IGNORECASE,
)
_COMPOUND_HOUSE_NUMBER = re.compile(r"(?:^|, )\d+[а-яa-z]?/\d+[а-яa-z]?$")
_HOUSE_SET_OR_PROJECT = re.compile(
    _OFFER + r"(?:(?:(?:единый )?лот из )?"
    r"(?:два|двух|три|тр[её]х|четыре|четыр[её]х|несколько|[2-9]|[1-9][0-9]+)\s+"
    r"(?:дом(?:а|ов)|коттедж(?:а|ей))|"
    r"(?:типов(?:ой|ого)\s+)?проект\s+(?:дома|коттеджа)|строительство\b|"
    r"(?:земельный\s+)?участок\s+без\s+дома|только (?:земельный )?участок)\b",
    re.IGNORECASE,
)
_OFFERED_HOUSE_PART = re.compile(
    _OFFER
    + _EXCLUSIVE_OFFER
    + rf"(?:(?:половин[ау]|часть)\s+(?:дом[ае]|коттедж[ае])|{_BLOCK_SCOPE})\b",
    re.IGNORECASE,
)
_DORMITORY = re.compile(
    r"\b(?:здание общежития|(?:статус|назначение) общежитие)\b", re.IGNORECASE
)
_OFFERED_HOUSE = _OFFER + rf"(?:дом|коттедж|дуплекс)\b(?:\s+№\s*{_BUILDING_NUMBER})?"
_BUILDING_MEASUREMENT = r"[, :]+(?:(?:общей )?площадью\s+)?"
_HOUSE_SIZE = re.compile(
    rf"(?:{_OFFERED_HOUSE}|{_CLAUSE}(?:(?:одно|двух)этажный )?дуплекс)"
    + _BUILDING_MEASUREMENT
    + _SQUARE_METRES,
    re.IGNORECASE,
)

_SECOND_OFFERED_HOUSE = re.compile(
    _OFFER + r"(?:дом|коттедж|единый\s+лот)\b"
    r"(?:(?![.!?;]\s)[^\n])*?\s+и\s+"
    r"(?:[её]щ[её]\s+один|второй)\s+(?:дом|коттедж)\b"
    r"(?!\s+(?:по соседству|отдельно))",
    re.IGNORECASE,
)
_LAND_SCALES = {"сотка": 100, "м²": 1, "га": 10_000}
_LAND_SIZE = re.compile(
    rf"(?:{_CLAUSE}площадь (?:земельного )?участка\s*[:—-]?\s*"
    rf"|{_OFFERED_HOUSE}(?:{_BUILDING_MEASUREMENT}{_AMOUNT}\s*{_SQUARE_METRE_UNIT})?"
    rf"\s+на (?:земельном )?участке\s+)"
    rf"(?P<area>{_AMOUNT})\s*(?P<unit>сот(?:ок|к[аи])|м[²2]|га)(?!\w)",
    re.IGNORECASE,
)


def land_square_metres(value: object, unit: object) -> Decimal:
    if not isinstance(unit, str) or unit not in _LAND_SCALES:
        raise RejectedCard("invalid_land_unit")
    return decimal_2(
        number(value) * _LAND_SCALES[unit],
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12"),
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
    offer_ends: list[int] = []
    for match in _COUNTED_ROOMS.finditer(text):
        count = match[1].casefold()
        if _NON_OFFERED_UNIT.match(text, match.end()):
            continue
        offer_ends.append(match.end())
        if count == "несколько":
            multiple = True
        elif count in _COUNTS:
            counts.add(_COUNTS[count])
        elif (published_count := optional_integer(count, minimum=1)) is not None:
            counts.add(published_count)
    for match in _ROOM_LIST.finditer(text):
        if _NON_OFFERED_UNIT.match(text, match.end()):
            continue
        identifiers = re.findall(_ROOM_ID, match["rooms"])
        if len(set("".join(label.split()) for label in identifiers)) != len(
            identifiers
        ):
            raise RejectedCard("conflicting_room_count")
        counts.add(len(identifiers))
        offer_ends.append(match.end())
    shares = list(_OFFERED_SHARE.finditer(text))
    if shares and any(
        not _NON_OFFERED_UNIT.match(text, denial.end())
        for denial in _SEPARATE_DENIAL.finditer(text)
    ):
        raise RejectedCard("unconfirmed_room_unit")
    for share in shares:
        published = _ROOM_ADDRESS.search(address or "")
        allocation = _ALLOCATED_ROOM.match(text, share.end())
        offered_clause = _CLAUSE_END.split(text[share.start() :], maxsplit=1)[0]
        if (
            not published
            or not allocation
            or published[1] != allocation[1]
            or _CONTRADICTORY_ALLOCATION.search(offered_clause)
        ):
            raise RejectedCard("unconfirmed_room_unit")
        counts.add(1)
    if len(counts) > 1 or (multiple and counts == {1}) or 0 in counts:
        raise RejectedCard("conflicting_room_count")
    detected = next(iter(counts), None)
    if multiple or (detected is not None and detected > 1):
        totals = _published_areas(_ROOM_SET_AREA_CLAUSE, text)
        for end in offer_ends:
            while (total := _ROOM_SET_AREA.match(text, end)) is not None:
                if not _NON_OFFERED_UNIT.match(text, total.end()):
                    totals.add(number(total["area"]))
                end = total.end()
        if len(totals) > 1:
            raise RejectedCard("conflicting_room_area")
        total_area = next(iter(totals), None)
        # The structural roomArea is for one room, never the offered set.
        return detected, (
            decimal_2(total_area, minimum=Decimal("0.01"), maximum=Decimal("1e10"))
            if total_area is not None
            else None
        )
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
    land_area: Decimal | None,
) -> None:
    text = description or ""
    if (
        _DORMITORY.search(text)
        or _HOUSE_SET_OR_PROJECT.search(text)
        or _SECOND_OFFERED_HOUSE.search(text)
    ):
        raise RejectedCard("unsupported_house_unit")
    if _OFFERED_SHARE.search(text):
        raise RejectedCard("unallocated_house_share")
    published = (address or "").casefold()
    block = _INDEPENDENT_BLOCK.search(published)
    markers = _BLOCK_MARKER.findall(published)
    if len(markers) > 1 or (
        not block and (markers or _OFFERED_HOUSE_PART.search(text))
    ):
        raise RejectedCard("unconfirmed_house_block")
    for offer in _OFFERED_ADDRESS.finditer(text):
        scope = offer["scope"].casefold()
        if scope in ("блок", "секция", "секцию"):
            known = block
        elif scope == "участок":
            known = _PLOT_ADDRESS.search(published)
        else:
            known = _HOUSE_ADDRESS.search(published)
        if known and offer["identifier"].casefold() != known["identifier"].casefold():
            raise RejectedCard("conflicting_house_unit")
        if scope in ("блок", "секция", "секцию") and not block:
            raise RejectedCard("unconfirmed_house_block")

    # A building's total area is incomparable to its separately offered section.
    areas = _published_areas(_BLOCK_SIZE if block else _HOUSE_SIZE, text)
    if area is not None and areas - {area}:
        raise RejectedCard("conflicting_house_area")
    plots = set() if land_area is None else {land_area}
    for statement in _LAND_SIZE.finditer(text):
        if _NON_OFFERED_UNIT.match(text, statement.end()):
            continue
        unit = statement["unit"].casefold()
        unit = "сотка" if unit.startswith("сот") else unit.replace("2", "²")
        plots.add(land_square_metres(statement["area"], unit))
    if len(plots) > 1:
        raise RejectedCard("conflicting_land_area")
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
