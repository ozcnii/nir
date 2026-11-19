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
_BUNDLE_COUNT_WORDS = {"обеих": 2, "двух": 2, "трех": 3, "трёх": 3}
_OFFERED = r"(?:продаваем|предлагаем|сдаваем)"
_ROOM_NUMBER = r"№\s*(?P<room>[1-9][0-9]*)"
_ROOM_SIZE = rf"(?:\s+(?:(?P<total>общей )?площадью )?(?P<area>{_MEASUREMENT})\s+м²)?"
_ROOM = re.compile(
    r"(?:доля:\s*)?(?:(?P<count>[1-9][0-9]*|одна|две|три)\s+)?"
    rf"(?P<unit>комнат[ауы]?)\b(?:\s+{_ROOM_NUMBER})?{_ROOM_SIZE}"
)
_NUMBERED_MEMBER = re.compile(_ROOM_NUMBER + _ROOM_SIZE)
_NEXT_MEMBER = re.compile(r"(?:,\s*(?:и\s+)?|\s+и\s+)(?:ещ[её]\s+)?")
_BUNDLE_AREA = re.compile(
    r"(?:,\s*|\s+)(?:одним лотом(?:,\s*|\s+))?(?:и\s+)?"
    r"(?:(?:общей|суммарной) площадью|(?:общая|суммарная) площадь)"
    rf" (?P<area>{_MEASUREMENT}) м²"
)
_OUTSIDE_OFFER = re.compile(
    r"\s*(?:[,—-]\s*)?(?:(?P<other_owner>соседская|соседей|принадлежит соседям)"
    r"|(?P<separate>по отдельности|не одним лотом)|отдельно|не входит"
    r"|на фото|в соседней квартире)\b"
)
_BUNDLE = re.compile(r"(?<!не )\bодним лотом\b")
_OFFER = re.compile(
    r"(?:прода[её]тся|продаются|продаю|продам|сда[её]тся|сдаются|сдаю|сдам"
    r"|предлагается|предлагаются|предлагаю)\s+"
)
_SENTENCE_BREAK = re.compile(r"[\r\n]+|[.!?;](?:\s+|$)")
_ASSERTION_BREAK = re.compile(r"[,:]\s*|\s*[-–—]\s*")
_ASSERTION_START = re.compile(
    rf"{_OFFER.pattern}|(?:(?:общая|суммарная|жилая) )?площадь [а-яё]"
    r"|участок\b|(?:эта |предлагаемая )?доля\b"
)
_OFFERED_ROOM_AREA = re.compile(
    rf"площадь {_OFFERED}ой комнаты"
    rf"(?: № ?(?P<room>[1-9][0-9]*))?(?:[: ]+|[ —-]+)(?P<area>{_MEASUREMENT}) м²"
)
_OFFERED_BUNDLE_AREA = re.compile(
    rf"(?:общая|суммарная) площадь (?:набора|{_OFFERED}ых"
    rf"|(?:этих(?: (?P<count>двух|тр[её]х|[1-9][0-9]*))?|(?P<both>обеих))"
    rf"(?: {_OFFERED}ых)?) комнат"
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
    r"дом\b(?: №\s*(?P<identifier>[0-9]+[а-я]?(?:[/-][0-9]+[а-я]?)*))?"
    rf"(?: (?:(?:(?P<area_role>общей|жилой) )?площадью )?(?P<area>{_MEASUREMENT}) м²"
    rf"{_AFTER_AREA_ROLE})?"
    rf"(?: на участке (?P<land>{_MEASUREMENT} (?:{_LAND_UNIT})))?"
)
_HOUSE_MEASURE = re.compile(
    rf"(?:(?P<role>общая|жилая) )?"
    rf"(?:площадь (?:(?:этого|{_OFFERED}ого) )?(?P<part>дома|участка)|участок)"
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
    r"(?:^|[,;]\s*|\s+[-–—]\s+"
    r"|\bдом\s+(?:№\s*)?[0-9]+[а-я]?(?:[/-][0-9]+[а-я]?)*\s+)"
    rf"{_BLOCK_NAME}\b(?=\s*(?:[0-9№,;]|$))"
)
_LISTED_HOUSE = re.compile(
    r"(?:(?:один|второй|третий|четв[её]ртый) )?" + _HOUSE_TITLE.pattern
)
_OFFER_CORRECTION = re.compile(r"[\s,:–—-]+(?:точнее|а именно)\b[\s,:–—-]*")
_OTHER_BUILDING = re.compile(r"\bв (?:соседнем|другом) (?:доме|здании)\b")


class RoomClaim(NamedTuple):
    rooms: int | None
    areas: tuple[Decimal, ...]
    member_ids: frozenset[str]
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
    for raw in _SENTENCE_BREAK.split(description.casefold()):
        clause = " ".join(raw.split())
        start = 0
        for delimiter in _ASSERTION_BREAK.finditer(clause):
            if _ASSERTION_START.match(clause, delimiter.end()):
                yield clause[start : delimiter.start()]
                start = delimiter.end()
        yield clause[start:]


def _seller_statements(description: str) -> Iterator[tuple[str, bool]]:
    for value in _seller_clauses(description):
        offer = _OFFER.match(value)
        if value:
            yield (value[offer.end() :] if offer else value, offer is not None)


def _room_claim(text: str) -> RoomClaim | None:
    first = _ROOM.match(text)
    if first is None or _OUTSIDE_OFFER.match(text, first.end()):
        return None
    count = first["count"]
    if count is None:
        rooms = 1 if first["unit"] in ("комната", "комнату") else None
    else:
        rooms = _COUNT_WORDS.get(count)
        if rooms is None:
            rooms = optional_integer(count, minimum=1)

    members = [first]
    end = first.end()
    # A room number can stand for a repeated noun within one published list.
    while (rooms == 1 or first["room"] is not None) and (
        link := _NEXT_MEMBER.match(text, end)
    ) is not None:
        member = _ROOM.match(text, link.end())
        if member is None:
            member = _NUMBERED_MEMBER.match(text, link.end())
            if member is None:
                break
        elif member["unit"] not in ("комната", "комнату") or member["count"] not in (
            None,
            "одна",
            "1",
        ):
            break
        outside = _OUTSIDE_OFFER.match(text, member.end())
        if outside is not None:
            if outside["separate"]:
                if rooms != 1:
                    return None
                members, end = [first], first.end()
            break
        members.append(member)
        end = member.end()

    identified: dict[str, set[Decimal]] = {}
    for member in members:
        if member_id := member["room"]:
            measurements = identified.setdefault(member_id, set())
            if member["area"]:
                measurements.add(number(member["area"]))
            if len(measurements) > 1:
                raise RejectedCard("conflicting_room_area")
    distinct_members = len(identified) + sum(
        member["room"] is None for member in members
    )
    if len(members) > 1:
        if rooms not in (None, 1, distinct_members):
            raise RejectedCard("conflicting_room_count")
        rooms = distinct_members
    areas: list[Decimal] = []
    if rooms == 1:
        areas.extend(number(member["area"]) for member in members if member["area"])
    elif (
        members[-1]["total"]
        and members[-1]["area"]
        and not any(member["area"] for member in members[:-1])
    ):
        # A total can follow a list of names. Member measurements stay individual.
        areas.append(number(members[-1]["area"]))
    if rooms is not None and (rooms > 1 or len(members) > 1):
        while (total := _BUNDLE_AREA.match(text, end)) is not None and not (
            _OUTSIDE_OFFER.match(text, total.end())
        ):
            areas.append(number(total["area"]))
            end = total.end()
    return RoomClaim(
        rooms,
        tuple(areas),
        frozenset(identified),
        count is not None or len(members) > 1,
    )


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
        and (bool(heading.member_ids) or heading.explicit_count)
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
                if claim is None or len(claim.member_ids) != 1:
                    raise RejectedCard("unconfirmed_room_unit")
                denied_rooms.update(claim.member_ids)
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
            if reference := (measure["count"] or measure["both"]):
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
    scope = {member for claim in claims for member in claim.member_ids}
    if (rooms is not None and len(scope) > rooms) or (share and scope & denied_rooms):
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
        subject, _, _ = statement.partition(", ")
        if _OTHER_BUILDING.search(subject):
            continue
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
            record_house(house)
            if not bundle:
                continue
            end = house.end()
            while (link := _NEXT_MEMBER.match(statement, end)) is not None:
                member = _LISTED_HOUSE.match(statement, link.end())
                if member is None or _OUTSIDE_OFFER.match(statement, member.end()):
                    break
                if (
                    house["identifier"] is None
                    or member["identifier"] != house["identifier"]
                ):
                    raise RejectedCard("unconfirmed_house_unit")
                record_house(member)
                end = member.end()

    house_areas.discard(None)
    plot_areas.discard(None)
    if len(house_areas) > 1 or len(plot_areas) > 1:
        raise RejectedCard("conflicting_house_measurement")
