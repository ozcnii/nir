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
_OFFER = rf"(?:{_CLAUSE}|(?<=,)\s*){_DEAL}\s+"
_AMOUNT = r"[0-9]+(?:[.,][0-9]+)?"
_SQUARE_METRE_UNIT = r"(?:м[²2]|кв\.\s*м)(?!\w)"
_SQUARE_METRES = rf"(?P<area>{_AMOUNT})\s*{_SQUARE_METRE_UNIT}"
_ROOM_MEASURE_PREFIX = r"\s+(?:(?:общей )?площадью\s+)?"
_ROOM_SIZE = re.compile(
    rf"(?:{_OFFER}|{_CLAUSE})(?:1 |одна |одну |уютная )?комнат[ау]"
    r"(?: №?\s*[0-9]+)?(?: в квартире №?\s*[0-9]+)?"
    r"(?: (?:общей )?площадью)?[ :]+" + _SQUARE_METRES,
    re.IGNORECASE,
)
_NON_OFFERED_UNIT = re.compile(
    r"\s*(?:[,:—–-]\s*)?(?:(?:в|из|у)\s+(?:сосед|друг)\w*"
    r"|по соседству"
    r"|(?:можно\s+(?:купить|приобрести)\s+)?(?:отдельно|по отдельности)"
    r"|не (?:входит|одним лотом))\b",
    re.IGNORECASE,
)
_COUNTS = {"одна": 1, "одну": 1, "две": 2, "три": 3, "четыре": 4}
_ROOM_COUNT = rf"(?:[0-9]{{1,5}}|{'|'.join(_COUNTS)}|несколько)"
_ROOM_ID = r"№\s*([1-9][0-9]{0,4})(?!\d)"
_ROOM_MEMBER = rf"{_ROOM_ID}(?:{_ROOM_MEASURE_PREFIX}{_AMOUNT}\s*{_SQUARE_METRE_UNIT})?"
_ROOM_OFFER = re.compile(
    _OFFER + rf"(?:(?:единый\s+)?лот\s+из\s+)?(?:(?P<count>{_ROOM_COUNT})\s+)?"
    rf"комнат[ауые]?\b(?:\s*[:,]?\s*(?P<members>{_ROOM_MEMBER}"
    rf"(?:\s*(?:,\s*(?:и\s+)?|\s+и\s+)(?:комнат[ауые]\s+)?{_ROOM_MEMBER})*))?",
    re.IGNORECASE,
)
_ROOM_MEMBER_ID = re.compile(_ROOM_ID)
_ROOM_MEMBER_SIZE = re.compile(_ROOM_MEASURE_PREFIX + _SQUARE_METRES, re.IGNORECASE)
_IDENTIFIED_ROOM_AREA = re.compile(
    _CLAUSE + rf"(?:общая )?площадь (?:этой|предлагаемой|продаваемой|сдаваемой) "
    rf"комнаты\s+{_ROOM_ID}\s*[:—–-]?\s*" + _SQUARE_METRES,
    re.IGNORECASE,
)
_ROOM_SET_AREA = re.compile(
    r"\s*,?\s*(?:одним лотом(?:,\s*|\s+))?"
    r"(?:(?:общей|суммарной) площадью|(?:общая|суммарная) площадь)"
    r"\s*[:—-]?\s*" + _SQUARE_METRES,
    re.IGNORECASE,
)
_REFERENT_COUNTS = {
    "обеих": 2,
    "двух": 2,
    "трёх": 3,
    "трех": 3,
    "четырёх": 4,
    "четырех": 4,
}
_SET_REFERENT_COUNT = rf"(?:{'|'.join(_REFERENT_COUNTS)}|[0-9]{{1,5}})"
_ROOM_SET_AREA_CLAUSE = re.compile(
    _CLAUSE + r"(?:общая|суммарная) площадь "
    rf"(?:набора комнат|(?:(?:этих(?:\s+(?P<count>{_SET_REFERENT_COUNT}))?"
    r"|(?P<pair>обеих)|предлагаемых|продаваемых|сдаваемых)\s+)?комнат)"
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
    rf"(?:^|,\s*|(?=\bдом\s+))(?:дом\s+)?(?:№\s*)?"
    rf"(?:{_BUILDING_NUMBER}(?:\s+|\s*[-–—]\s*))?(?:{_BLOCK_SCOPE})\b",
)
_EXCLUSIVE_OFFER = r"(?:(?:только|лишь)\s+)?"
_INDEPENDENT_BLOCK = re.compile(
    rf"(?:^|, )(?:дом )?(?:№\s*)?{_BUILDING_NUMBER}, (?:{_BLOCK_SCOPE}) "
    rf"(?:№\s*)?(?P<identifier>{_BUILDING_NUMBER})$",
    re.IGNORECASE,
)
_HOUSE_ADDRESS = re.compile(
    rf"(?:^|, )(?:дом )?(?:№\s*)?(?P<identifier>{_BUILDING_NUMBER})"
    rf"(?=, (?:{_BLOCK_SCOPE}|участок)\b|$)",
    re.IGNORECASE,
)
_PLOT_ADDRESS = re.compile(
    rf"(?:^|, )участок (?:№\s*)?(?P<identifier>{_BUILDING_NUMBER})(?:,|$)",
    re.IGNORECASE,
)
_OFFERED_PLOT = re.compile(
    _OFFER + _EXCLUSIVE_OFFER + rf"участок\s+№\s*({_BUILDING_NUMBER})(?![\w/])",
    re.IGNORECASE,
)
_BLOCK_SIZE = re.compile(
    _CLAUSE + r"(?:общая )?площадь (?:блока|секции)\s*[:—-]?\s*" + _SQUARE_METRES,
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
    _OFFER + _EXCLUSIVE_OFFER + r"(?:половин[ау]|часть)\s+(?:дом[ае]|коттедж[ае])\b",
    re.IGNORECASE,
)
_DORMITORY = re.compile(
    r"\b(?:здание общежития|(?:статус|назначение) общежитие)\b", re.IGNORECASE
)
_HOUSE_UNIT = r"(?:дом|коттедж|дуплекс)\b"
_BUILDING_MEASUREMENT = r"[, :]+(?:(?:общей )?площадью\s+)?"
_HOUSE_SIZE = re.compile(
    _CLAUSE
    + r"(?:(?:одно|двух)этажный )?дуплекс"
    + _BUILDING_MEASUREMENT
    + _SQUARE_METRES,
    re.IGNORECASE,
)

_LAND_SCALES = {"сотка": 100, "м²": 1, "га": 10_000}
_PLOT_MEASUREMENT = (
    rf"(?P<land_area>{_AMOUNT})\s*(?P<land_unit>сот(?:ок|к[аи])|м[²2]|га)(?!\w)"
)
_LAND_SIZE = re.compile(
    _CLAUSE + r"площадь (?:(?:этого|предлагаемого|продаваемого|сдаваемого) )?"
    r"(?:земельного )?участка\s*[:—-]?\s*" + _PLOT_MEASUREMENT,
    re.IGNORECASE,
)
_BUILDING_FACTS = (
    r"(?:(?P<block>(?:(?:жил(?:ой|ая|ую)|самостоятельн(?:ый|ая|ую))\s+)?"
    rf"(?:{_BLOCK_SCOPE}))\b"
    rf"(?:\s+(?:№\s*)?(?P<block_id>{_BUILDING_NUMBER})(?![\w/]|[.,-][0-9]|\s*{_SQUARE_METRE_UNIT}))?"
    rf"|{_HOUSE_UNIT}(?:\s+№\s*(?P<house_id>{_BUILDING_NUMBER})(?![\w/]))?)"
    rf"(?:{_BUILDING_MEASUREMENT}{_SQUARE_METRES})?"
    rf"(?:\s+на (?:земельном )?участке(?:\s+{_PLOT_MEASUREMENT})?)?"
)
_BUILDING_OFFER = re.compile(
    _OFFER + _EXCLUSIVE_OFFER + rf"(?:{_HOUSE_UNIT}(?:\s+№\s*{_BUILDING_NUMBER})?"
    rf"(?:{_BUILDING_MEASUREMENT}{_AMOUNT}\s*{_SQUARE_METRE_UNIT})?"
    r"\s*(?:[,—–-]\s*)?\bточнее\b\s*(?:[,:—–-]\s*)?)?" + _BUILDING_FACTS,
    re.IGNORECASE,
)
_BUILDING_MEMBER = re.compile(
    r"(?:\s*,\s*(?:(?P<comma_join>и)\s+)?|\s+(?P<join>и)\s+)"
    r"(?:(?P<additional>[её]щ[её] один|второй|третий|четв[её]ртый)\s+)?"
    + _BUILDING_FACTS,
    re.IGNORECASE,
)
_JOINT_LOT = re.compile(r"\s*(?:,\s*)?(?:одним лотом|в том же лоте)\b", re.IGNORECASE)


def land_square_metres(value: object, unit: object) -> Decimal:
    if not isinstance(unit, str) or unit not in _LAND_SCALES:
        raise RejectedCard("invalid_land_unit")
    return decimal_2(
        number(value) * _LAND_SCALES[unit],
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12"),
    )


def _plot_square_metres(claim: re.Match[str]) -> Decimal:
    unit = claim["land_unit"].casefold()
    unit = "сотка" if unit.startswith("сот") else unit.replace("2", "²")
    return land_square_metres(claim["land_area"], unit)


def _published_areas(pattern: re.Pattern[str], text: str) -> set[Decimal]:
    return {
        number(match["area"])
        for match in pattern.finditer(text)
        if not _NON_OFFERED_UNIT.match(text, match.end())
    }


def _building_offers(text: str) -> list[re.Match[str]]:
    offers = []
    for claim in _BUILDING_OFFER.finditer(text):
        if _NON_OFFERED_UNIT.match(text, claim.end()):
            continue
        end = claim.end()
        attached = False
        joined = False
        while (member := _BUILDING_MEMBER.match(text, end)) is not None:
            end = member.end()
            if (detached := _NON_OFFERED_UNIT.match(text, end)) is not None:
                end = detached.end()
                continue
            attached = True
            joined |= bool(
                member["join"] or member["comma_join"] or member["additional"]
            )
        if attached and (joined or _JOINT_LOT.match(text, end)):
            raise RejectedCard("unsupported_house_unit")
        offers.append(claim)
    return offers


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
    detached: set[int] = set()
    member_sets: set[frozenset[str]] = set()
    member_areas: dict[str, set[Decimal]] = {}
    area_claims = {claim.start(): claim for claim in _ROOM_SIZE.finditer(text)}
    for match in _ROOM_OFFER.finditer(text):
        # Determine ownership after the described list or measure, not after
        # the room noun: a qualifier belongs to the quantified offer.
        end = match.end()
        if (unit_area := area_claims.get(match.start())) is not None:
            end = max(end, unit_area.end())
        if (total := _ROOM_SET_AREA.match(text, end)) is not None:
            end = total.end()
        if _NON_OFFERED_UNIT.match(text, end):
            detached.add(match.start())
            continue
        count = match["count"]
        members = match["members"]
        if count is None and members is None:
            continue
        offer_ends.append(match.end())
        if count is not None:
            token = count.casefold()
            if token == "несколько":
                multiple = True
            else:
                count_value = _COUNTS.get(token)
                if count_value is None:
                    count_value = optional_integer(token, minimum=1)
                if count_value is not None:
                    counts.add(count_value)
        if members is not None:
            numbered = list(_ROOM_MEMBER_ID.finditer(members))
            identifiers = set()
            for identifier in numbered:
                name = identifier[1]
                identifiers.add(name)
                measurements = member_areas.setdefault(name, set())
                if (
                    size := _ROOM_MEMBER_SIZE.match(members, identifier.end())
                ) is not None:
                    measurements.add(number(size["area"]))
            # One published member of an explicitly counted set does not
            # prove that the remaining members are absent.
            if len(numbered) > 1 or count is None:
                member_sets.add(frozenset(identifiers))
                counts.add(len(identifiers))
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
    if (
        len(counts) > 1
        or len(member_sets) > 1
        or (multiple and counts == {1})
        or 0 in counts
    ):
        raise RejectedCard("conflicting_room_count")
    if counts == {1} and (published := _ROOM_ADDRESS.search(address or "")) is not None:
        member_areas.setdefault(published[1], set())
    for claim in _IDENTIFIED_ROOM_AREA.finditer(text):
        if claim[1] in member_areas and not _NON_OFFERED_UNIT.match(text, claim.end()):
            member_areas[claim[1]].add(number(claim["area"]))
    if any(len(sizes) > 1 for sizes in member_areas.values()):
        raise RejectedCard("conflicting_room_area")
    detected = next(iter(counts), None)
    if multiple or (detected is not None and detected > 1):
        totals = set()
        for claim in _ROOM_SET_AREA_CLAUSE.finditer(text):
            referent = claim["pair"] or claim["count"]
            if referent is not None and detected is not None:
                count_value = _REFERENT_COUNTS.get(referent.casefold())
                if count_value is None:
                    count_value = int(referent)
                if count_value != detected:
                    continue
            if not _NON_OFFERED_UNIT.match(text, claim.end()):
                totals.add(number(claim["area"]))
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
    areas = {
        number(claim["area"])
        for claim in area_claims.values()
        if claim.start() not in detached
        and not _NON_OFFERED_UNIT.match(text, claim.end())
    }
    areas.update(size for sizes in member_areas.values() for size in sizes)
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
    if _DORMITORY.search(text) or _HOUSE_SET_OR_PROJECT.search(text):
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
    house = _HOUSE_ADDRESS.search(published)
    offers = _building_offers(text)
    for claim in offers:
        offers_block = claim["block"] is not None
        if offers_block and not block:
            raise RejectedCard("unconfirmed_house_block")
        known, identifier = (
            (block, claim["block_id"]) if offers_block else (house, claim["house_id"])
        )
        if (
            known
            and identifier
            and identifier.casefold() != known["identifier"].casefold()
        ):
            raise RejectedCard("conflicting_house_unit")

    # Whole-building measurements do not describe a separately offered section.
    scoped = [claim for claim in offers if (claim["block"] is not None) == bool(block)]
    areas = _published_areas(_BLOCK_SIZE if block else _HOUSE_SIZE, text)
    areas.update(number(claim["area"]) for claim in scoped if claim["area"])
    if area is not None:
        areas.add(area)
    plots = {
        _plot_square_metres(claim)
        for claim in _LAND_SIZE.finditer(text)
        if not _NON_OFFERED_UNIT.match(text, claim.end())
    }
    plots.update(_plot_square_metres(claim) for claim in scoped if claim["land_area"])
    if land_area is not None:
        plots.add(land_area)

    plot = _PLOT_ADDRESS.search(published)
    if plot and any(
        offer[1].casefold() != plot["identifier"].casefold()
        for offer in _OFFERED_PLOT.finditer(text)
    ):
        raise RejectedCard("conflicting_house_unit")
    if len(areas) > 1:
        raise RejectedCard("conflicting_house_area")
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
