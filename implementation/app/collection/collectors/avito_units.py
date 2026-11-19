"""Resolve the unit actually offered in a recognized Avito card."""

import re
from collections.abc import Iterator
from decimal import Decimal
from enum import Enum, auto
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
    r"(?<!\w)(?:доля:\s*)?(?:(?P<count>[1-9][0-9]*|одна|две|три)\s+)?"
    rf"(?P<unit>комнат[ауы]?)\b(?:\s+{_ROOM_NUMBER})?{_ROOM_SIZE}"
)
_NUMBERED_MEMBER = re.compile(_ROOM_NUMBER + _ROOM_SIZE)
_ROOM_DESIGNATION = re.compile(r",\s*" + _NUMBERED_MEMBER.pattern)
_NEXT_MEMBER = re.compile(r"(?:,\s*(?:и\s+)?|\s+и\s+)(?:ещ[её]\s+)?")
_BUNDLE_AREA = re.compile(
    r"(?:,\s*|\s+)(?:одним лотом(?:,\s*|\s+))?(?:и\s+)?"
    r"(?:(?:общей|суммарной) площадью|(?:общая|суммарная) площадь)"
    rf" (?P<area>{_MEASUREMENT}) м²"
)
_ROLE_SEPARATOR = r"\s*(?:[,—-]\s*)?(?<!\w)"
_OUTSIDE_OFFER = re.compile(
    _ROLE_SEPARATOR + r"(?:(?P<other_owner>соседская|соседей|принадлежит соседям)"
    r"|(?P<separate>по отдельности|не одним лотом)|отдельно|не входит"
    r"|на фото|в соседней квартире)\b"
)
_OUTSIDE_ROOM = re.compile(
    rf"{_OUTSIDE_OFFER.pattern}|{_ROLE_SEPARATOR}"
    r"в (?:другой квартире|(?:соседнем|другом) доме)\b"
)
_BUNDLE = re.compile(r"(?<!не )\bодним лотом\b")
_LOT_SUFFIX = re.compile(r"[\s,]+одним лотом\b")
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
    rf"(?:общая|суммарная) площадь (?:набора|(?P<own>{_OFFERED}ых)"
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
_BUILDING_NUMBER = r"(?: №\s*(?P<identifier>[0-9]+[а-я]?(?:[/-][0-9]+[а-я]?)*))?"
_BUILDING_MEASURES = (
    rf"(?: (?:(?:(?P<area_role>общей|жилой) )?площадью )?(?P<area>{_MEASUREMENT}) м²"
    rf"{_AFTER_AREA_ROLE})?"
    rf"(?: на участке (?P<land>{_MEASUREMENT} (?:{_LAND_UNIT})))?"
)
_HOUSE_TITLE = re.compile(r"дом\b" + _BUILDING_NUMBER + _BUILDING_MEASURES)
_HOUSE_MEASURE = re.compile(
    rf"(?:(?P<role>общая|жилая) )?"
    rf"(?:площадь (?:(?:этого|{_OFFERED}ого) )?(?P<part>дома|участка)|участок)"
    r"(?:[: ]+|[ —-]+)(?:(?P<before>общая|жилая) )?"
    rf"(?P<value>{_MEASUREMENT} (?:{_LAND_UNIT}))(?=\s|[.,;(–—-]|$)"
    rf"{_AFTER_AREA_ROLE}"
)
_BLOCK_NAME = r"(?:блок|секци[яю])"
_BLOCK_QUALIFIER = r"(?:(?:жил|отдельн|самостоятельн)(?:ой|ый|ая|ую) )?"
_HOUSE_NONSINGLE = re.compile(
    r"(?:только |лишь )?"
    r"(?:(?:[2-9]|[1-9][0-9]+|два|три|четыре|несколько) (?:дома|домов)"
    r"|(?:[0-9]+/[0-9]+ )?(?:доля|часть|половина) (?:дома|в доме)"
    rf"|(?:типовой )?проект дома|земельный участок"
    rf"|{_BLOCK_QUALIFIER}{_BLOCK_NAME})\b"
)
_ADDRESS_BLOCK = re.compile(
    r"(?:^|[,;]\s*|\s+[-–—]\s+"
    r"|\bдом\s+(?:№\s*)?[0-9]+[а-я]?(?:[/-][0-9]+[а-я]?)*\s+)"
    rf"{_BLOCK_NAME}\b(?=\s*(?:[0-9№,;]|$))"
)
_LISTED_BUILDING = re.compile(
    rf"(?:(?:(?:один|второй|третий|четв[её]ртый) )?дом"
    rf"|{_BLOCK_QUALIFIER}(?P<block>{_BLOCK_NAME}))\b"
    + _BUILDING_NUMBER
    + _BUILDING_MEASURES
)
_OFFER_CORRECTION = re.compile(r"[\s,:–—-]+(?:точнее|а именно)\b[\s,:–—-]*")
_OTHER_BUILDING = re.compile(r"\bв (?:соседнем|другом) (?:доме|здании)\b")


class RoomMember(NamedTuple):
    identifier: str | None
    areas: tuple[Decimal, ...]
    marked_total: bool


class RoomClaim(NamedTuple):
    rooms: int | None
    bundle: bool
    areas: tuple[Decimal, ...]
    member_areas: dict[str, set[Decimal]]
    explicit_count: bool


class RoomSubject(Enum):
    OFFERED = auto()
    OTHER_OFFER = auto()
    OTHER_MENTION = auto()
    UNKNOWN = auto()


class RoomEvidence(NamedTuple):
    offers: list[RoomClaim]
    member_sizes: list[re.Match[str]]
    bundle_totals: list[re.Match[str]]
    allocated: bool
    denied_rooms: set[str]


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


def _read_room_member(text: str, match: re.Match[str]) -> tuple[RoomMember, int]:
    suffix = (
        _ROOM_DESIGNATION.match(text, match.end()) if match["room"] is None else None
    )
    claims = (match, suffix) if suffix is not None else (match,)
    end = suffix or match
    areas = tuple(number(claim["area"]) for claim in claims if claim["area"])
    return RoomMember(
        end["room"], areas, any(claim["total"] for claim in claims)
    ), end.end()


def _room_qualifier(text: str, end: int) -> re.Match[str] | None:
    lot = _LOT_SUFFIX.match(text, end)
    return _OUTSIDE_ROOM.match(text, lot.end() if lot else end)


def _room_claim(text: str) -> RoomClaim | None:
    first = _ROOM.match(text)
    if first is None:
        return None
    designation = (
        _ROOM_DESIGNATION.match(text, first.end()) if not first["room"] else None
    )
    if _room_qualifier(text, (designation or first).end()):
        return None
    first_member, first_end = _read_room_member(text, first)
    count = first["count"]
    if count is None:
        rooms = 1 if first["unit"] in ("комната", "комнату") else None
    else:
        rooms = _COUNT_WORDS.get(count)
        if rooms is None:
            rooms = optional_integer(count, minimum=1)

    members = [first_member]
    end = first_end
    while (rooms == 1 or first_member.identifier is not None) and (
        link := _NEXT_MEMBER.match(text, end)
    ) is not None:
        member = _ROOM.match(text, link.end())
        if member is None:
            # An omitted noun continues an established numbered list.
            if members[-1].identifier is None:
                break
            member = _NUMBERED_MEMBER.match(text, link.end())
            if member is None:
                break
        elif member["unit"] not in ("комната", "комнату") or member["count"] not in (
            None,
            "одна",
            "1",
        ):
            break
        next_member, next_end = _read_room_member(text, member)
        outside = _room_qualifier(text, next_end)
        if outside is not None:
            if _LOT_SUFFIX.match(text, next_end):
                return None
            if outside["separate"]:
                if rooms != 1:
                    return None
                members, end = [first_member], first_end
            break
        members.append(next_member)
        end = next_end

    identified: dict[str, set[Decimal]] = {
        member.identifier: set() for member in members if member.identifier is not None
    }
    distinct_members = len(identified) + sum(
        member.identifier is None for member in members
    )
    if len(members) > 1:
        if rooms not in (None, 1, distinct_members):
            raise RejectedCard("conflicting_room_count")
        rooms = distinct_members
    # An uncounted plural offer establishes the scope of a total.
    bundle = rooms != 1
    areas: list[Decimal] = []
    total_after_names = (
        bundle
        and members[-1].marked_total
        and len(members[-1].areas) == 1
        and not any(member.areas for member in members[:-1])
    )
    for index, member_scope in enumerate(members):
        if total_after_names and index == len(members) - 1:
            areas.extend(member_scope.areas)
        elif member_scope.identifier is not None:
            identified[member_scope.identifier].update(member_scope.areas)
        elif rooms == 1:
            areas.extend(member_scope.areas)
    if bundle or len(members) > 1:
        while (total := _BUNDLE_AREA.match(text, end)) is not None:
            if _room_qualifier(text, total.end()):
                return None
            areas.append(number(total["area"]))
            end = total.end()
    return RoomClaim(
        rooms,
        bundle,
        tuple(areas),
        identified,
        count is not None or len(members) > 1,
    )


def _room_evidence(description: str, *, share: bool) -> RoomEvidence:
    offered: list[RoomClaim] = []
    member_sizes: list[re.Match[str]] = []
    bundle_totals: list[re.Match[str]] = []
    leading_member_sizes: list[re.Match[str]] = []
    leading_bundle_totals: list[re.Match[str]] = []
    subject: RoomSubject | None = None
    competing_subject = False
    qualified_by_heading = False
    denied_rooms: set[str] = set()
    allocated = False
    for statement, offer in _seller_statements(description):
        member_size = _OFFERED_ROOM_AREA.match(statement)
        bundle_total = _OFFERED_BUNDLE_AREA.match(statement)
        measurement = member_size or bundle_total
        qualifier = _OUTSIDE_ROOM.match(statement)
        body = statement[qualifier.end() :].lstrip(" ,:—–-") if qualifier else statement
        outside = qualified_by_heading or qualifier is not None
        qualified_by_heading = qualifier is not None and not body
        if (measurement and _room_qualifier(statement, measurement.end())) or (
            outside and (qualified_by_heading or _ROOM.search(body))
        ):
            competing_subject = True
            if offer or _OFFER.match(body) or measurement:
                subject = RoomSubject.OTHER_OFFER
            elif subject not in (RoomSubject.OTHER_OFFER, RoomSubject.UNKNOWN):
                subject = RoomSubject.OTHER_MENTION
            continue

        if share and _UNALLOCATED.match(statement):
            raise RejectedCard("unconfirmed_room_unit")
        if share and (room := _ROOM.match(statement)) is not None:
            room_qualifier = _room_qualifier(statement, room.end())
            if room_qualifier and room_qualifier["other_owner"] and room["room"]:
                denied_rooms.add(room["room"])

        if share and (allocation := _ROOM_ALLOCATION.match(statement)) is not None:
            claim = _room_claim(statement[allocation.end() :])
            if allocation["not_matched"] or allocation["not_assigned"]:
                if claim is None or len(claim.member_areas) != 1:
                    raise RejectedCard("unconfirmed_room_unit")
                denied_rooms.update(claim.member_areas)
            elif claim is not None and claim.rooms == 1:
                offered.append(claim)
                allocated = True
                subject = RoomSubject.OFFERED
            continue

        if offer or _BUNDLE.search(statement):
            if (claim := _room_claim(statement)) is not None:
                offered.append(claim)
                subject = RoomSubject.OFFERED
                continue
            if _ROOM.match(statement) and _OUTSIDE_ROOM.search(statement):
                competing_subject = True
                subject = RoomSubject.OTHER_OFFER
                continue

        if member_size:
            if subject is RoomSubject.OFFERED:
                member_sizes.append(member_size)
            elif subject is None:
                leading_member_sizes.append(member_size)
        elif bundle_total:
            # A demonstrative stays attached to the current subject.
            # An unqualified "offered" can distinguish an offer from a mention.
            if (subject is RoomSubject.OFFERED and offered[-1].bundle) or (
                subject is RoomSubject.OTHER_MENTION and bundle_total["own"]
            ):
                bundle_totals.append(bundle_total)
            elif subject is None:
                leading_bundle_totals.append(bundle_total)
        elif _ROOM.search(statement):
            competing_subject = True
            if not _OUTSIDE_ROOM.search(statement):
                subject = RoomSubject.UNKNOWN
            elif subject not in (RoomSubject.OTHER_OFFER, RoomSubject.UNKNOWN):
                subject = RoomSubject.OTHER_MENTION

    # A leading reference can only resolve forward without a competing subject.
    if not competing_subject:
        member_sizes.extend(leading_member_sizes)
        bundle_totals.extend(leading_bundle_totals)
    return RoomEvidence(offered, member_sizes, bundle_totals, allocated, denied_rooms)


def rooms_and_area(
    title: str, description: str, area: Decimal | None, *, share: bool
) -> tuple[int | None, Decimal | None]:
    label = title.casefold()
    heading = _room_claim(label)
    share = share or label.startswith("доля")
    offered, member_sizes, bundle_totals, allocated, denied_rooms = _room_evidence(
        description, share=share
    )
    allocated = allocated or (
        heading is not None
        and label.startswith("доля:")
        and (bool(heading.member_areas) or heading.explicit_count)
    )

    bundle = any(claim.bundle for claim in offered) or (
        not offered
        and heading is not None
        and heading.bundle
        and bool(
            heading.explicit_count
            or heading.areas
            or heading.member_areas
            or _BUNDLE.search(label)
        )
    )
    counts = {claim.rooms for claim in offered if claim.rooms is not None}
    if (
        heading is not None
        and heading.rooms is not None
        and (heading.explicit_count or (not counts and heading.bundle == bundle))
    ):
        counts.add(heading.rooms)
    if len(counts) > 1 or (bundle and counts == {1}):
        raise RejectedCard("conflicting_room_count")
    rooms = next(iter(counts), None)
    if share and (rooms != 1 or not allocated):
        raise RejectedCard("unconfirmed_room_unit")

    claims = [
        claim
        for claim in [heading, *offered]
        if claim is not None and claim.bundle == bundle
    ]
    member_areas: dict[str, set[Decimal]] = {}
    for claim in claims:
        for member_id, measurements in claim.member_areas.items():
            member_areas.setdefault(member_id, set()).update(measurements)
    scope = member_areas.keys()
    if (rooms is not None and len(scope) > rooms) or (share and scope & denied_rooms):
        raise RejectedCard("unconfirmed_room_unit")
    areas = {amount for claim in claims for amount in claim.areas}
    for measurement in member_sizes:
        measured_id = measurement["room"]
        if measured_id is not None and measured_id in scope:
            member_areas[measured_id].add(number(measurement["area"]))
        elif rooms == 1 and (measured_id is None or not scope):
            areas.add(number(measurement["area"]))
    if any(len(measurements) > 1 for measurements in member_areas.values()):
        raise RejectedCard("conflicting_room_area")
    if bundle:
        for measure in bundle_totals:
            if rooms is not None and (reference := measure["count"] or measure["both"]):
                referred_count = _BUNDLE_COUNT_WORDS.get(reference)
                if referred_count is None:
                    referred_count = optional_integer(reference, minimum=2)
                if referred_count is not None and referred_count != rooms:
                    raise RejectedCard("conflicting_room_count")
            areas.add(number(measure["area"]))
    else:
        if area is not None:
            areas.add(area)
        if rooms == 1:
            areas.update(
                amount
                for measurements in member_areas.values()
                for amount in measurements
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
        house = _HOUSE_TITLE.match(statement) if offer or bundle else None
        if bundle and house is not None and _NEXT_MEMBER.match(statement, house.end()):
            # The next member's location does not reclassify the first subject.
            subject = statement[: house.end()]
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
        elif house is not None:
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
                member = _LISTED_BUILDING.match(statement, link.end())
                if member is None or _OUTSIDE_OFFER.match(statement, member.end()):
                    break
                if (
                    member["block"]
                    or house["identifier"] is None
                    or member["identifier"] != house["identifier"]
                ):
                    raise RejectedCard("unconfirmed_house_unit")
                record_house(member)
                end = member.end()

    house_areas.discard(None)
    plot_areas.discard(None)
    if len(house_areas) > 1 or len(plot_areas) > 1:
        raise RejectedCard("conflicting_house_measurement")
