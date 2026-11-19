"""Resolve the unit actually offered in a recognized Avito card."""

import re
from collections.abc import Iterator
from decimal import Decimal
from typing import NamedTuple

from app.collection.normalization import (
    RejectedCard,
    decimal_2,
    number,
    optional_integer,
)

_LAND_UNIT = r"м²|сот(?:\.|ок|ки|ка)|га"
_AREA = re.compile(rf"(.+?)\s+({_LAND_UNIT})")
_MEASUREMENT = r"[0-9]+(?:[.,][0-9]+)?"
_COUNT_WORDS = {"одна": 1, "две": 2, "три": 3}
_BUNDLE_COUNT_WORDS = {"двух": 2, "трех": 3, "трёх": 3}
_ROOM = re.compile(
    r"(?:доля:\s*)?(?:(?P<count>[1-9][0-9]*|одна|две|три)\s+)?"
    r"(?P<unit>комнат[ауы]?)\b(?:\s+№\s*(?P<room>[1-9][0-9]*))?"
    rf"(?:\s+(?:(?P<total>общей )?площадью )?(?P<area>{_MEASUREMENT})\s+м²)?"
)
_NEXT_MEMBER = re.compile(r"(?:,\s*(?:и\s+)?|\s+и\s+)(?:ещ[её]\s+)?")
_BUNDLE_AREA = re.compile(
    r"(?:,\s*|\s+)(?:одним лотом(?:,\s*|\s+))?(?:и\s+)?"
    rf"(?:общей площадью|общая площадь) (?P<area>{_MEASUREMENT}) м²"
)
_OUTSIDE_OFFER = re.compile(
    r"\s*(?:[,—-]\s*)?(?:(?P<other_owner>соседская|соседей|принадлежит соседям)"
    r"|отдельно|по отдельности|не одним лотом|не входит|на фото|в соседней квартире)\b"
)
_BUNDLE = re.compile(r"(?<!не )\bодним лотом\b")
_OFFER = re.compile(
    r"(?:прода[её]тся|продаются|продаю|продам|сда[её]тся|сдаются|сдаю|сдам"
    r"|предлагается|предлагаются|предлагаю)\s+"
)
_CLAUSE = re.compile(
    rf"[\r\n]+|[.!?;](?:\s+|$)|[,:]\s+"
    rf"(?={_OFFER.pattern}|(?:общая |жилая )?площадь\b"
    r"|участок\b|(?:эта |предлагаемая )?доля\b)"
)
_OFFERED_ROOM_AREA = re.compile(
    r"площадь (?:продаваемой|предлагаемой|сдаваемой) комнаты"
    rf"(?: № ?(?P<room>[1-9][0-9]*))?(?:[: ]+|[ —-]+)(?P<area>{_MEASUREMENT}) м²"
)
_OFFERED_BUNDLE_AREA = re.compile(
    r"общая площадь (?:набора|продаваемых|предлагаемых|сдаваемых"
    r"|этих (?P<count>двух|тр[её]х|[1-9][0-9]*)) комнат"
    rf"\b[\s:–—-]+(?P<area>{_MEASUREMENT}) м²"
)
_ROOM_ALLOCATION = re.compile(
    r"(?:(?:этой|предлагаемой) доле (?P<not_matched>не )?соответствует"
    r"|за (?:этой|предлагаемой) долей (?P<not_assigned>не )?закреплена)\s+"
)
_UNALLOCATED = re.compile(
    r"(?:(?:(?:эта|предлагаемая) )?доля(?: [0-9]+/[0-9]+)?\s+)?"
    r"(?:без выделен(?:ия|ной комнаты)|не выделен[ао])\b"
)

_AFTER_AREA_ROLE = (
    r"(?:(?:\s*[-–—]\s*|\s+)(?P<after>общая|жилая)\b(?: площадь)?"
    r"|\s*\((?P<parenthetical>общая|жилая)(?: площадь)?\))?"
)
_HOUSE_TITLE = re.compile(
    rf"дом(?: (?:(?P<area_role>общей|жилой) площадью )?(?P<area>{_MEASUREMENT}) м²"
    rf"{_AFTER_AREA_ROLE})?"
    rf"(?: на участке (?P<land>{_MEASUREMENT} (?:{_LAND_UNIT})))?"
)
_HOUSE_MEASURE = re.compile(
    r"(?:(?P<role>общая|жилая) )?(?:площадь (?P<part>дома|участка)|участок)"
    r"(?:[: ]+|[ —-]+)(?:(?P<before>общая|жилая) )?"
    rf"(?P<value>{_MEASUREMENT} (?:{_LAND_UNIT}))(?=\s|[.,;(–—-]|$)"
    rf"{_AFTER_AREA_ROLE}"
)
_BLOCK_NAME = r"(?:блок|секци[яю])"
_HOUSE_NONSINGLE = re.compile(
    r"(?:только |лишь )?"
    r"(?:(?:[2-9]|[1-9][0-9]+|два|три|четыре|несколько) (?:дома|домов)"
    r"|(?:[0-9]+/[0-9]+ )?(?:доля|часть|половина) (?:дома|в доме)"
    rf"|(?:типовой )?проект дома|земельный участок"
    rf"|(?:(?:жил|отдельн|самостоятельн)(?:ой|ый|ая|ую) )?{_BLOCK_NAME})\b"
)
_ADDRESS_BLOCK = re.compile(
    r"(?:^|[,;]\s*|\bдом\s+(?:№\s*)?[0-9]+[а-я]?(?:[/-][0-9]+[а-я]?)*\s+)"
    rf"{_BLOCK_NAME}\b(?=\s*(?:[0-9№,;]|$))"
)
_NEXT_HOUSE = re.compile(r"(?:(?:второй|ещ[её] один) )?дом\b")
_OFFER_CORRECTION = re.compile(r"\s*[,–—-]\s*(?:точнее|а именно)(?:[,:]\s*|\s+)")
_DASH = re.compile(r"\s+[-–—]\s+")
_INDEPENDENT_CLAIMS = (
    _OFFER,
    _OFFERED_ROOM_AREA,
    _OFFERED_BUNDLE_AREA,
    _HOUSE_MEASURE,
    _UNALLOCATED,
    _ROOM_ALLOCATION,
)


class RoomClaim(NamedTuple):
    rooms: int | None
    areas: tuple[Decimal, ...]
    room: str | None
    explicit_count: bool


def area_m2(value: str | None, *, land: bool = False) -> Decimal | None:
    if value is None:
        return None
    match = _AREA.fullmatch(value)
    if match is None or (not land and match[2] != "м²"):
        raise RejectedCard("invalid_area_unit")
    scale = 4 if match[2] == "га" else 2 if match[2].startswith("сот") else 0
    sign, digits, exponent = number(match[1]).as_tuple()
    assert isinstance(exponent, int)
    return decimal_2(
        Decimal((sign, digits, exponent + scale)),
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12" if land else "1e10"),
    )


def _seller_clauses(description: str) -> Iterator[str]:
    for raw in _CLAUSE.split(description.casefold()):
        clause = " ".join(raw.split())
        start = 0
        for delimiter in _DASH.finditer(clause):
            if any(
                claim.match(clause, delimiter.end()) for claim in _INDEPENDENT_CLAIMS
            ):
                yield clause[start : delimiter.start()]
                start = delimiter.end()
        yield clause[start:]


def _seller_statements(description: str) -> Iterator[tuple[str, bool]]:
    for value in _seller_clauses(description):
        offer = _OFFER.match(value)
        if value:
            yield (value[offer.end() :] if offer else value, offer is not None)


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
    room = match["room"]
    identified_members = {room} if room is not None else set()
    areas: list[Decimal] = []
    if match["area"] and (rooms == 1 or match["total"]):
        areas.append(number(match["area"]))
    members = 1
    while rooms == 1 and (link := _NEXT_MEMBER.match(text, end)) is not None:
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
        member_id = member["room"]
        if member_id is None or member_id not in identified_members:
            members += 1
        if member_id is not None:
            identified_members.add(member_id)
        if member["area"]:
            areas.append(number(member["area"]))
        end = member.end()
    count_is_explicit = count is not None
    if members > 1:
        rooms, room, count_is_explicit = members, None, True
        areas.clear()
    if rooms is not None and (rooms > 1 or end > match.end()):
        while (total := _BUNDLE_AREA.match(text, end)) is not None and not (
            _OUTSIDE_OFFER.match(text, total.end())
        ):
            areas.append(number(total["area"]))
            end = total.end()
    return RoomClaim(rooms, tuple(areas), room, count_is_explicit)


def rooms_and_area(
    title: str, description: str, area: Decimal | None, *, share: bool
) -> tuple[int | None, Decimal | None]:
    label = title.casefold()
    heading = _room_claim(label)
    share = share or label.startswith("доля")
    offered: list[RoomClaim] = []
    explicit_areas: list[tuple[str | None, Decimal]] = []
    bundle_areas: set[Decimal] = set()
    bundle_counts: set[int] = set()
    denied_rooms: set[str] = set()
    allocated = (
        heading is not None
        and label.startswith("доля:")
        and (heading.room is not None or heading.explicit_count)
    )
    for statement, offer in _seller_statements(description):
        if share and _UNALLOCATED.match(statement):
            raise RejectedCard("unconfirmed_room_unit")
        if share and (room := _ROOM.match(statement)) is not None:
            outside = _OUTSIDE_OFFER.match(statement, room.end())
            if outside and outside["other_owner"] and room["room"]:
                denied_rooms.add(room["room"])
        if share and (allocation := _ROOM_ALLOCATION.match(statement)) is not None:
            claim = _room_claim(statement[allocation.end() :])
            if allocation["not_matched"] or allocation["not_assigned"]:
                if claim is None or claim.room is None:
                    raise RejectedCard("unconfirmed_room_unit")
                denied_rooms.add(claim.room)
            elif claim is not None and claim.rooms == 1:
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
        elif (
            measure := _OFFERED_BUNDLE_AREA.match(statement)
        ) is not None and not _OUTSIDE_OFFER.match(statement, measure.end()):
            bundle_areas.add(number(measure["area"]))
            if reference := measure["count"]:
                referred_count = _BUNDLE_COUNT_WORDS.get(reference)
                if referred_count is None:
                    referred_count = optional_integer(reference, minimum=2)
                if referred_count is not None:
                    bundle_counts.add(referred_count)

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
    if rooms == 1 and (len(scope) > 1 or (share and scope & denied_rooms)):
        raise RejectedCard("unconfirmed_room_unit")
    areas = {amount for claim in claims for amount in claim.areas}
    if rooms in (None, 1):
        if area is not None:
            areas.add(area)
        if rooms == 1:
            areas.update(
                amount
                for room, amount in explicit_areas
                if not scope or room is None or room in scope
            )
    else:
        if bundle_counts - {rooms}:
            raise RejectedCard("conflicting_room_count")
        areas.update(bundle_areas)
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
    address: str,
) -> None:
    heading = _HOUSE_TITLE.fullmatch(title.casefold())
    if heading is None or share or _ADDRESS_BLOCK.search(address.casefold()):
        raise RejectedCard("unconfirmed_house_unit")
    house_areas = {area}
    plot_areas = {land}

    def record_house(claim: re.Match[str]) -> None:
        if (
            claim["area"]
            and claim["area_role"] != "жилой"
            and "жилая" not in (claim["after"], claim["parenthetical"])
        ):
            house_areas.add(area_m2(f"{claim['area']} м²"))
        if claim["land"]:
            plot_areas.add(area_m2(claim["land"], land=True))

    record_house(heading)
    for statement, offer in _seller_statements(description):
        bundle = _BUNDLE.search(statement) is not None
        if _UNALLOCATED.match(statement) or (
            (offer or bundle) and _HOUSE_NONSINGLE.match(statement)
        ):
            raise RejectedCard("unconfirmed_house_unit")

        if (measured := _HOUSE_MEASURE.match(statement)) is not None:
            if _OUTSIDE_OFFER.match(statement, measured.end()) or "жилая" in (
                measured["role"],
                measured["before"],
                measured["after"],
                measured["parenthetical"],
            ):
                continue
            if measured["part"] == "дома":
                house_areas.add(area_m2(measured["value"]))
            else:
                plot_areas.add(area_m2(measured["value"], land=True))
        elif (offer or bundle) and (house := _HOUSE_TITLE.match(statement)) is not None:
            if _OUTSIDE_OFFER.match(statement, house.end()):
                continue
            correction = _OFFER_CORRECTION.match(statement, house.end())
            if correction and _HOUSE_NONSINGLE.match(statement, correction.end()):
                raise RejectedCard("unconfirmed_house_unit")
            continuation = _NEXT_MEMBER.match(statement, house.end())
            if (
                bundle
                and continuation
                and _NEXT_HOUSE.match(statement, continuation.end())
            ):
                raise RejectedCard("unconfirmed_house_unit")
            record_house(house)

    house_areas.discard(None)
    plot_areas.discard(None)
    if len(house_areas) > 1 or len(plot_areas) > 1:
        raise RejectedCard("conflicting_house_measurement")
