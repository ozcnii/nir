"""Resolve the unit actually offered in a recognized Avito card."""

import re
from decimal import Decimal
from typing import NamedTuple

from app.collection.normalization import (
    RejectedCard,
    decimal_2,
    number,
    optional_integer,
)

_AREA = re.compile(r"(.+?)\s+(м²|сот\.|га)")
_MEASUREMENT = r"[0-9]+(?:[.,][0-9]+)?"
_COUNT_WORDS = {"одна": 1, "две": 2, "три": 3}
_ROOM = re.compile(
    r"(?:доля:\s*)?(?:(?P<count>[1-9][0-9]*|одна|две|три)\s+)?"
    r"(?P<unit>комнат[ауы]?)\b(?:\s+№\s*(?P<room>[1-9][0-9]*))?"
    rf"(?:\s+(?:(?P<total>общей )?площадью )?(?P<area>{_MEASUREMENT})\s+м²)?"
)
_NEXT_ROOM = re.compile(r",?\s+и\s+(?:ещ[её]\s+)?")
_OUTSIDE_OFFER = re.compile(
    r"\s*(?:[,—-]\s*)?(?:отдельно|не входит|на фото|в соседней квартире|соседей|принадлежит соседям)\b"
)
_CLAUSE = re.compile(r"[\r\n]+|[.!?;](?:\s+|$)")
_BUNDLE = re.compile(r"\bодним лотом\b")
_OFFER = re.compile(
    r"(?:прода[её]тся|продаются|продаю|продам|сда[её]тся|сдаются|сдаю|сдам"
    r"|предлагается|предлагаются|предлагаю)\s+"
)
_OFFERED_ROOM_AREA = re.compile(
    r"площадь (?:продаваемой|предлагаемой|сдаваемой) комнаты"
    rf"(?: № ?(?P<room>[1-9][0-9]*))?(?:[: ]+|[ —-]+)(?P<area>{_MEASUREMENT}) м²"
)
_ROOM_ALLOCATION = re.compile(
    r"(?:(?:этой|предлагаемой) доле соответствует"
    r"|за (?:этой|предлагаемой) долей закреплена)\s+"
)
_UNALLOCATED = re.compile(
    r"(?:(?:(?:эта|предлагаемая) )?доля(?: [0-9]+/[0-9]+)?\s+)?"
    r"(?:без выделения|не выделен[ао])\b"
)

_HOUSE_TITLE = re.compile(
    rf"дом(?: (?:общей площадью )?(?P<area>{_MEASUREMENT}) м²)?"
    rf"(?: на участке (?P<land>{_MEASUREMENT} (?:м²|сот\.|га)))?"
)
_HOUSE_AREA = re.compile(
    rf"(?:общая )?площадь дома(?:[: ]+|[ —-]+)(?P<area>{_MEASUREMENT}) м²"
)
_HOUSE_NONSINGLE = re.compile(
    r"(?:(?:[2-9]|[1-9][0-9]+|два|три|четыре|несколько) (?:дома|домов)"
    r"|дом и (?:ещ[её] один )?дом|(?:[0-9]+/[0-9]+ )?(?:доля|часть|половина) (?:дома|в доме)"
    r"|(?:типовой )?проект дома|земельный участок)\b"
)


class RoomClaim(NamedTuple):
    rooms: int | None
    area: Decimal | None
    room: str | None
    explicit_count: bool


def area_m2(value: str | None, *, land: bool = False) -> Decimal | None:
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


def _seller_statements(description: str) -> list[tuple[str, bool]]:
    claims = []
    for clause in _CLAUSE.split(description.casefold()):
        value = " ".join(clause.split())
        offer = _OFFER.match(value)
        if value:
            claims.append((value[offer.end() :] if offer else value, offer is not None))
    return claims


def _room_claim(text: str) -> RoomClaim | None:
    match = _ROOM.match(text)
    if match is None or _OUTSIDE_OFFER.match(text, match.end()):
        return None
    count = match["count"]
    if count is None:
        rooms = 1 if match["unit"] in ("комната", "комнату") else None
    else:
        rooms = _COUNT_WORDS.get(count)
        if rooms is None:
            rooms = optional_integer(count, minimum=1)
    end = match.end()
    members = 1
    while (link := _NEXT_ROOM.match(text, end)) is not None:
        if (member := _ROOM.match(text, link.end())) is None or _OUTSIDE_OFFER.match(
            text, member.end()
        ):
            break
        if member["unit"] not in ("комната", "комнату") or member["count"] not in (
            None,
            "одна",
            "1",
        ):
            break
        members += 1
        end = member.end()
    if members > 1 and rooms == 1:
        return RoomClaim(members, None, None, True)
    area = (
        area_m2(f"{match['area']} м²")
        if match["area"] and (rooms == 1 or match["total"])
        else None
    )
    return RoomClaim(rooms, area, match["room"], count is not None)


def rooms_and_area(
    title: str, description: str, area: Decimal | None, *, share: bool
) -> tuple[int | None, Decimal | None]:
    label = title.casefold()
    heading = _room_claim(label)
    share = share or label.startswith("доля")
    offered: list[RoomClaim] = []
    explicit_areas: list[tuple[str | None, Decimal]] = []
    allocated = (
        heading is not None
        and label.startswith("доля:")
        and (heading.room is not None or heading.explicit_count)
    )
    for statement, offer in _seller_statements(description):
        if share and _UNALLOCATED.match(statement):
            raise RejectedCard("unconfirmed_room_unit")
        if (allocation := _ROOM_ALLOCATION.match(statement)) is not None and share:
            claim = _room_claim(statement[allocation.end() :])
            if claim is not None and claim.rooms == 1:
                offered.append(claim)
                allocated = True
        elif (offer or _BUNDLE.search(statement)) and (
            claim := _room_claim(statement)
        ) is not None:
            offered.append(claim)
        elif (
            measure := _OFFERED_ROOM_AREA.match(statement)
        ) is not None and not _OUTSIDE_OFFER.match(statement, measure.end()):
            explicit_areas.append((measure["room"], number(measure["area"])))

    counts = {claim.rooms for claim in offered if claim.rooms is not None}
    if (
        heading is not None
        and heading.rooms is not None
        and (not counts or heading.explicit_count)
    ):
        counts.add(heading.rooms)
    if len(counts) > 1:
        raise RejectedCard("conflicting_room_count")
    rooms = next(iter(counts), None)
    if share and (rooms != 1 or not allocated):
        raise RejectedCard("unconfirmed_room_unit")

    claims = [
        claim
        for claim in [heading, *offered]
        if claim is not None and claim.rooms == rooms
    ]
    scope = {claim.room for claim in claims if claim.room is not None}
    if rooms == 1 and len(scope) > 1:
        raise RejectedCard("unconfirmed_room_unit")
    areas = {claim.area for claim in claims if claim.area is not None}
    if rooms in (None, 1):
        if area is not None:
            areas.add(area)
        if rooms == 1:
            areas.update(
                amount
                for room, amount in explicit_areas
                if not scope or room is None or room in scope
            )
    if len(areas) > 1:
        raise RejectedCard("conflicting_room_area")
    normalized = next(iter(areas), None)
    if normalized is not None:
        normalized = decimal_2(
            normalized, minimum=Decimal("0.01"), maximum=Decimal("1e10")
        )
    return rooms, normalized


def check_house(
    title: str,
    description: str,
    area: Decimal | None,
    land: Decimal | None,
    *,
    share: bool,
) -> None:
    heading = _HOUSE_TITLE.fullmatch(title.casefold())
    if heading is None or share:
        raise RejectedCard("unconfirmed_house_unit")
    measurements = [heading]
    for statement, offer in _seller_statements(description):
        if _UNALLOCATED.match(statement) or (
            (offer or _BUNDLE.search(statement)) and _HOUSE_NONSINGLE.match(statement)
        ):
            raise RejectedCard("unconfirmed_house_unit")
        if (measurement := _HOUSE_AREA.match(statement)) is not None:
            if not _OUTSIDE_OFFER.match(statement, measurement.end()):
                measurements.append(measurement)
        elif offer and (measurement := _HOUSE_TITLE.match(statement)) is not None:
            if not _OUTSIDE_OFFER.match(statement, measurement.end()):
                measurements.append(measurement)
    measured = {number(value["area"]) for value in measurements if value["area"]}
    if area is not None:
        measured.add(area)
    if len(measured) > 1 or (
        land is not None
        and heading["land"]
        and area_m2(heading["land"], land=True) != land
    ):
        raise RejectedCard("conflicting_house_measurement")
