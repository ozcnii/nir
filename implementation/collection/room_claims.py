"""Resolve published claims about the offered room or room set."""

import re
from decimal import Decimal
from typing import NamedTuple

from collection.normalization import RejectedCard, decimal_2, number, optional_integer

_AMOUNT = r"[0-9]+(?:[.,][0-9]+)?"
_UNIT = r"м[²2](?=\s|[.,;!?]|$)"
_ROOM = r"комнат[ауы]"
_WORD_COUNTS = {"одна": "1", "одну": "1", "две": "2", "три": "3"}
_OFFER = re.compile(
    r"^(?:прода[её]тся|продаются|продаю|прода[её]м|продам|сда[её]тся|сдаются|сдаю|сда[её]м|сдам) +"
)
_COUNT = re.compile(
    rf"^(?P<count>[1-9][0-9]*|{'|'.join(_WORD_COUNTS)}) +комнат(?:а|у|ы)?\b"
)
_IDENTIFIER = r"[0-9]+[а-я]?(?:[-/][0-9]+[а-я]?)*\b"
_PAUSE = r" *[,—–-] *"
_ROOM_TAG = rf"№ *(?P<room>{_IDENTIFIER})"
_APARTMENT_LOCATION = rf"(?: +|{_PAUSE})в квартире +(?:№ *)?"
_APARTMENT = rf"(?:{_APARTMENT_LOCATION}(?P<apartment>{_IDENTIFIER}))?"
_ROOM_FRAGMENT = re.compile(
    rf"(?P<single>отдельн(?:ая|ую) +)?(?P<noun>{_ROOM})\b"
    rf"(?: +{_ROOM_TAG})?{_APARTMENT}"
    rf"(?:(?: +(?:площадью +)?|{_PAUSE}площадью +| *[—–] +)"
    rf"(?P<area>{_AMOUNT}) *{_UNIT})?"
)
_ROOM_NUMBER = re.compile(rf"{_ROOM_TAG}{_APARTMENT}")
_ROOM_LINK = re.compile(
    r"(?: *,? +и +|(?P<comma> *, *))(?P<extra>ещ[её] +)?(?:(?:одна|одну|1) +)?"
)
_LIST_INTRO = re.compile(r"^ *[:,—–-] *")
_TOTAL = re.compile(
    rf" *(?:, *)?(?P<whole>общей +)?площадью +(?P<area>{_AMOUNT}) *{_UNIT}"
)
_SHARE = re.compile(r"^(?:[0-9]+/[0-9]+ +)?дол[яю]\b")
_ALLOCATED_UNIT = r"(?=(?:комната +№ ?[1-9][0-9]*|(?:одна|1) комната)\b)"
_ALLOCATED = re.compile(
    r"^(?:доля: |(?:[0-9]+/[0-9]+ +)?дол[яю], которо[йю] "
    rf"(?:по соглашению )?(?P<negated>не +)?соответствует ){_ALLOCATED_UNIT}"
)
_ALLOCATION_VERB = r"закреплена|соответствует"
_RELATED_ALLOCATION = re.compile(
    r"^(?:(?P<holders>(?:не +)?за +.+?)"
    r"|(?:этой|предлагаемой) доле|по соглашению(?: о пользовании)?)"
    rf"[ ,—–-]+(?P<negated>не +)?(?:{_ALLOCATION_VERB}) +{_ALLOCATED_UNIT}"
)
_ROOM_FIRST_ALLOCATION = re.compile(
    rf"^[ ,—–-]*(?P<negated>не +)?(?:{_ALLOCATION_VERB})\b"
)
_HOLDER_TERM = re.compile(r"^[ ,—–-]*(?P<negated>не +)?(?:за|принадлежит) +")
_SOLD_HOLDER = re.compile(r"^(?:(?:этой|предлагаемой) долей|ней|продавц(?:ом|у))\b")
_ALTERNATIVE = re.compile(r" *,? +а +")
_HOLDER_ROOM = re.compile(
    rf"(?:{_PAUSE}|(?: +|{_PAUSE})(?P<negated>не +)?(?:{_ALLOCATION_VERB}) +)"
    rf"(?={_ROOM}\b)"
)
_ALLOCATION_OWNER = re.compile(r"^[ ,—–-]*(?:не +)?принадлежит\b")
_NOT_ALLOCATED = re.compile(r"\b(?:без выделения|не выделен[ао]?)\b")
_OTHER_SCOPE = re.compile(
    r"^[ ,—-]*(?:в соседне[йм]|в друго[йм]|у сосед|не прода|не сда)"
)
_IMAGE = r"на (?:[а-я0-9-]+ +){0,2}(?:фото|фотографи[ия]х?|плане|схеме)\b"
_DEPICTED = re.compile(rf"^(?:(?: +| *[—–-] *){_IMAGE}| *, *{_IMAGE}(?=$|[,;]))")
_SEPARATE = re.compile(
    r"^[ ,—–-]*(?:котор(?:ую|ые) +)?"
    r"(?:(?:прода[её]тся|сда[её]тся|можно купить|можно снять)"
    rf"(?:{_APARTMENT_LOCATION}(?P<before>{_IDENTIFIER}))? +)?"
    rf"(?:отдельно|не входит)\b{_APARTMENT}"
)
_AREA_STATEMENT = re.compile(
    rf"^площадь (?:продаваемой|предлагаемой|сдаваемой) комнаты\b"
    rf"(?: +{_ROOM_TAG})?{_APARTMENT}"
    rf"(?: *[:=—–-] *| +(?:составляет +)?)(?P<area>{_AMOUNT}) *{_UNIT}"
)
_CARD_REFERENCE = re.compile(
    r"^\s*(?:арт\.|артикул|объявление) *№? *[0-9]+[.\n]\s*", re.IGNORECASE
)
_CLAUSE_END = re.compile(r"[.!?;](?:\s+|$)|\n+")
_SQUARE_METERS = re.compile(r"\bкв\.\s+м\b", re.IGNORECASE)


def room_heading(title: str) -> bool:
    """Check the advertised heading independently of the route category."""
    text = _OFFER.sub("", title)
    return (
        _SHARE.match(text) is not None
        or _COUNT.match(text) is not None
        or _ROOM_FRAGMENT.match(text) is not None
    )


def _statements(value: str) -> list[str]:
    text = _SQUARE_METERS.sub("м²", value).translate(str.maketrans("()", ",,"))
    return _CLAUSE_END.split(text)


def _outside_offer(tail: str) -> bool:
    return bool(_OTHER_SCOPE.match(tail) or _DEPICTED.match(tail))


def _enumerated_rooms(claim: str, first: re.Match[str]) -> tuple[int, int]:
    rooms, end = 1, first.end()
    numbered_list = first["room"] is not None and first["noun"] == "комнаты"
    identifiers: dict[str, set[str | None]] = (
        {first["room"]: {first["apartment"]}} if first["room"] else {}
    )
    while link := _ROOM_LINK.match(claim, end):
        member = _ROOM_FRAGMENT.match(claim, link.end())
        if member is not None:
            if link["comma"] and not (
                link["extra"] or member["room"] or member["area"]
            ):
                break
            numbered_list = member["room"] is not None and member["noun"] == "комнаты"
        elif numbered_list:
            member = _ROOM_NUMBER.match(claim, link.end())
        if member is None:
            break
        tail = claim[member.end() :]
        if _outside_offer(tail) or _SEPARATE.match(tail):
            break
        if member["room"]:
            apartment = member["apartment"]
            seen_in = identifiers.setdefault(member["room"], set())
            if seen_in and (
                apartment is None or None in seen_in or apartment in seen_in
            ):
                raise RejectedCard("conflicting_room_count")
            seen_in.add(apartment)
        rooms, end = rooms + 1, member.end()
    return rooms, end


def _set_area(tail: str, *, after_count: bool = False) -> Decimal | None:
    total = _TOTAL.match(tail)
    if total is None or not (after_count or total["whole"]):
        return None
    return None if _outside_offer(tail[total.end() :]) else number(total["area"])


def _listing_after_count(tail: str) -> tuple[str, re.Match[str]] | None:
    intro = _LIST_INTRO.match(tail)
    if intro is None:
        return None
    listing = tail[intro.end() :]
    first = _ROOM_FRAGMENT.match(listing)
    return (listing, first) if first is not None else None


type RoomRef = tuple[str, str | None]


class RoomClaim(NamedTuple):
    rooms: int | None = None
    area: Decimal | None = None
    total: Decimal | None = None
    room: RoomRef | None = None


def _room_ref(match: re.Match[str]) -> RoomRef | None:
    return (match["room"], match["apartment"]) if match["room"] else None


def _compatible_rooms(left: RoomRef, right: RoomRef) -> bool:
    return left[0] == right[0] and (
        left[1] is None or right[1] is None or left[1] == right[1]
    )


def _separate_room(member: re.Match[str], separate: re.Match[str]) -> RoomRef | None:
    apartments = {
        apartment
        for apartment in (
            member["apartment"],
            separate["before"],
            separate["apartment"],
        )
        if apartment is not None
    }
    if member["room"] and len(apartments) == 1:
        return member["room"], next(iter(apartments))
    return None


def _claim_fields(claim: str) -> RoomClaim:
    count = _COUNT.match(claim)
    if count:
        rooms = optional_integer(_WORD_COUNTS.get(count[1], count[1]), minimum=1)
        tail = claim[count.end() :]
        if _outside_offer(tail):
            return RoomClaim()
        if declared := _listing_after_count(tail):
            listing, first_listed = declared
            if not _outside_offer(listing[first_listed.end() :]):
                listed, end = _enumerated_rooms(listing, first_listed)
                if listed != rooms:
                    raise RejectedCard("conflicting_room_count")
                if listed > 1:
                    return RoomClaim(rooms=listed, total=_set_area(listing[end:]))
                listed_area = first_listed["area"]
                return RoomClaim(
                    1,
                    number(listed_area) if listed_area else None,
                    room=_room_ref(first_listed),
                )
        if rooms != 1:
            return RoomClaim(rooms=rooms, total=_set_area(tail, after_count=True))
        claim = f"комната{tail}"

    first = _ROOM_FRAGMENT.match(claim)
    if first is None or _outside_offer(claim[first.end() :]):
        return RoomClaim()
    rooms, end = _enumerated_rooms(claim, first)
    if rooms > 1:
        return RoomClaim(rooms=rooms, total=_set_area(claim[end:]))
    if _SEPARATE.match(claim[first.end() :]):
        return RoomClaim()
    if area := first["area"]:
        return RoomClaim(1, number(area), room=_room_ref(first))
    known_single = count or (
        (first["single"] or first["room"]) and first["noun"] != "комнаты"
    )
    return RoomClaim(1, room=_room_ref(first)) if known_single else RoomClaim()


def _holder_facts(
    text: str,
    evidence: RoomClaim,
    *,
    implicit_holder: bool = False,
    first_predicate_negated: bool = False,
) -> list[tuple[RoomClaim, bool]]:
    facts = []
    for index, term in enumerate(_ALTERNATIVE.split(text)):
        negated = index == 0 and first_predicate_negated
        confirmed: bool | None = True if index == 0 and implicit_holder else None
        if member := _ROOM_FRAGMENT.match(term):
            evidence = _claim_fields(term)
            term = term[member.end() :]
            if predicate := _ROOM_FIRST_ALLOCATION.match(term):
                negated |= predicate["negated"] is not None
                term = term[predicate.end() :]
        if holder := _HOLDER_TERM.match(term):
            holder_text = term[holder.end() :]
            sold_holder = _SOLD_HOLDER.match(holder_text) is not None
            if member_after_holder := _HOLDER_ROOM.search(holder_text):
                evidence = _claim_fields(holder_text[member_after_holder.end() :])
                negated |= member_after_holder["negated"] is not None
            holder_negated = holder["negated"] is not None
            confirmed = (
                sold_holder and not holder_negated
                if sold_holder or not holder_negated
                else None
            )
        if negated:
            confirmed = False
        if confirmed is not None:
            facts.append((evidence, confirmed))
    return facts


def _share_allocation(
    claim: str, *, share_heading: bool
) -> list[tuple[RoomClaim, bool]] | None:
    prefix = (_ALLOCATED if share_heading else _RELATED_ALLOCATION).match(claim)
    if prefix is not None:
        claim = claim[prefix.end() :]
    member = _ROOM_FRAGMENT.search(claim) if prefix else _ROOM_FRAGMENT.match(claim)
    if member is None:
        return None

    tail = claim[member.end() :]
    predicate = _ROOM_FIRST_ALLOCATION.match(tail)
    if prefix is None and predicate is None and not _ALLOCATION_OWNER.match(tail):
        return None

    evidence = _claim_fields(claim)
    if evidence.rooms != 1 or (prefix is None and evidence.room is None):
        raise RejectedCard("unconfirmed_room_unit")
    before = prefix["holders"] if prefix and not share_heading else None
    after = tail[predicate.end() :] if predicate else tail
    negated = bool(
        (prefix and prefix["negated"]) or (predicate and predicate["negated"])
    )
    preceding = _holder_facts(before, evidence) if before and not negated else []
    following = _holder_facts(
        after,
        evidence,
        implicit_holder=prefix is not None and before is None,
        first_predicate_negated=negated,
    )
    return [*preceding, *following]


def room_facts(
    rooms: int | None,
    room_area: Decimal | None,
    description: str | None,
    *,
    title: str = "",
    share: bool = False,
) -> tuple[int | None, Decimal | None]:
    """Keep counts and areas scoped to a single proposed physical unit."""
    counts = set() if rooms is None else {rooms}
    measurements: list[tuple[RoomRef | None, Decimal]] = []
    totals: set[Decimal] = set()
    offered: set[RoomRef] = set()
    separate: set[RoomRef] = set()
    allocations: set[RoomRef | None] = set()
    disputed_allocations: set[RoomRef | None] = set()
    headings = _statements(title)
    sources = [*headings, *_statements(_CARD_REFERENCE.sub("", description or ""))]
    published = []
    for index, source in enumerate(sources):
        claim = " ".join(source.casefold().split())
        offer = _OFFER.match(claim)
        if offer:
            claim = claim[offer.end() :].removeprefix("только ")
        share_claim = _SHARE.match(claim)
        if share_claim and _OTHER_SCOPE.match(claim[share_claim.end() :]):
            continue
        share_heading = share_claim is not None
        published.append((index, claim, offer is not None, share_heading))
        share |= share_heading

    for index, claim, offer_stated, share_heading in published:
        if measurement := _AREA_STATEMENT.match(claim):
            if not _outside_offer(claim[measurement.end() :]):
                measurements.append(
                    (_room_ref(measurement), number(measurement["area"]))
                )
            continue
        member = _ROOM_FRAGMENT.match(claim)
        if member and (separate_sale := _SEPARATE.match(claim[member.end() :])):
            if room := _separate_room(member, separate_sale):
                separate.add(room)
            continue
        if share_heading and _NOT_ALLOCATED.search(claim):
            raise RejectedCard("unconfirmed_room_unit")
        allocation = (
            _share_allocation(claim, share_heading=share_heading) if share else None
        )
        if allocation is None:
            if share_heading:
                continue
            if not offer_stated and index >= len(headings):
                if index != len(headings):
                    continue
                if claim.startswith("уютная "):
                    claim = claim.removeprefix("уютная ")
                elif not (
                    (declared := _COUNT.match(claim))
                    and _listing_after_count(claim[declared.end() :])
                ):
                    continue
            evidence = _claim_fields(claim)
            if evidence.room is not None:
                offered.add(evidence.room)
            accepted = [evidence]
        else:
            accepted = []
            for evidence, confirmed in allocation:
                if evidence.rooms != 1:
                    raise RejectedCard("unconfirmed_room_unit")
                if confirmed:
                    allocations.add(evidence.room)
                    accepted.append(evidence)
                else:
                    disputed_allocations.add(evidence.room)
        for evidence in accepted:
            if evidence.rooms is not None:
                counts.add(evidence.rooms)
            if evidence.area is not None:
                measurements.append((evidence.room, evidence.area))
            if evidence.total is not None:
                totals.add(evidence.total)

    if share:
        if not allocations or any(
            room is None or disputed is None or _compatible_rooms(room, disputed)
            for room in allocations
            for disputed in disputed_allocations
        ):
            raise RejectedCard("unconfirmed_room_unit")
        offered.update(room for room in allocations if room is not None)
        counts.add(1)
    if len(counts) > 1:
        raise RejectedCard("conflicting_room_count")
    count = next(iter(counts), None)
    if count == 1:
        for room in offered:
            if any(not _compatible_rooms(room, other) for other in offered):
                reason = "unconfirmed_room_unit" if share else "conflicting_room_count"
                raise RejectedCard(reason)
    if count is not None and count > 1:
        comparable = totals
    else:
        candidates = [
            (room, value)
            for room, value in measurements
            # A separate sale cannot erase evidence about the offered room.
            if room not in separate
            or any(_compatible_rooms(room, subject) for subject in offered)
        ]
        subjects = offered or {room for room, _ in candidates if room is not None}
        comparable = {room_area} if room_area is not None else set()
        comparable.update(
            value
            for room, value in candidates
            if room is None
            or all(_compatible_rooms(room, subject) for subject in subjects)
        )
    if len(comparable) > 1:
        raise RejectedCard("conflicting_room_area")
    area = next(iter(comparable), None)
    if area is not None:
        area = decimal_2(area, minimum=Decimal("0.01"), maximum=Decimal("1e10"))
    return count, area
