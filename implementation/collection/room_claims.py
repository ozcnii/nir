"""Resolve published claims about the offered room or room set."""

import re
from decimal import Decimal

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
_APARTMENT = rf"(?:(?: +|{_PAUSE})в квартире +(?:№ *)?(?P<apartment>{_IDENTIFIER}))?"
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
    rf"(?:по соглашению )?соответствует ){_ALLOCATED_UNIT}"
)
_RELATED_ALLOCATION = re.compile(
    r"^(?:за (?:ней|(?:этой|предлагаемой) долей)|по соглашению(?: о пользовании)?)"
    rf" +закреплена +{_ALLOCATED_UNIT}"
)
_NOT_ALLOCATED = re.compile(r"\b(?:без выделения|не выделен[ао]?)\b")
_OTHER_SCOPE = re.compile(
    r"^[ ,—-]*(?:в соседне[йм]|в друго[йм]|у сосед|не прода|не сда)"
)
_IMAGE = r"на (?:[а-я0-9-]+ +){0,2}(?:фото|фотографи[ия]х?|плане|схеме)\b"
_DEPICTED = re.compile(rf"^(?:(?: +| *[—–-] *){_IMAGE}| *, *{_IMAGE}(?=$|[,;]))")
_SEPARATE = re.compile(
    r"^[ ,—–-]*(?:котор(?:ую|ые) +)?"
    r"(?:(?:прода[её]тся|сда[её]тся|можно купить|можно снять) +)?"
    r"(?:отдельно|не входит)\b"
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


def _claim_fields(claim: str) -> tuple[int | None, Decimal | None, Decimal | None]:
    count = _COUNT.match(claim)
    if count:
        rooms = optional_integer(_WORD_COUNTS.get(count[1], count[1]), minimum=1)
        tail = claim[count.end() :]
        if _outside_offer(tail):
            return None, None, None
        if declared := _listing_after_count(tail):
            listing, first_listed = declared
            if not _outside_offer(listing[first_listed.end() :]):
                listed, end = _enumerated_rooms(listing, first_listed)
                if listed != rooms:
                    raise RejectedCard("conflicting_room_count")
                if listed > 1:
                    return listed, None, _set_area(listing[end:])
                listed_area = first_listed["area"]
                return 1, number(listed_area) if listed_area else None, None
        if rooms != 1:
            return rooms, None, _set_area(tail, after_count=True)
        claim = f"комната{tail}"

    first = _ROOM_FRAGMENT.match(claim)
    if first is None or _outside_offer(claim[first.end() :]):
        return None, None, None
    rooms, end = _enumerated_rooms(claim, first)
    if rooms > 1:
        return rooms, None, _set_area(claim[end:])
    if area := first["area"]:
        return 1, number(area), None
    known_single = count or (
        (first["single"] or first["room"]) and first["noun"] != "комнаты"
    )
    return (1 if known_single else None), None, None


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
    areas = set() if room_area is None else {room_area}
    totals: set[Decimal] = set()
    allocated = False
    headings = _statements(title)
    sources = [*headings, *_statements(_CARD_REFERENCE.sub("", description or ""))]
    for index, source in enumerate(sources):
        claim = " ".join(source.casefold().split())
        offer = _OFFER.match(claim)
        if offer:
            claim = claim[offer.end() :].removeprefix("только ")
        if measurement := _AREA_STATEMENT.match(claim):
            if not _outside_offer(claim[measurement.end() :]):
                areas.add(number(measurement["area"]))
            continue
        allocation = None
        if share_claim := _SHARE.match(claim):
            if _OTHER_SCOPE.match(claim[share_claim.end() :]):
                continue
            share = True
            if _NOT_ALLOCATED.search(claim):
                raise RejectedCard("unconfirmed_room_unit")
            allocation = _ALLOCATED.match(claim)
            if not allocation:
                continue
            claim = claim[allocation.end() :]
        elif share and (allocation := _RELATED_ALLOCATION.match(claim)):
            claim = claim[allocation.end() :]
        elif not offer and index >= len(headings):
            if index != len(headings):
                continue
            if claim.startswith("уютная "):
                claim = claim.removeprefix("уютная ")
            elif not (
                (declared := _COUNT.match(claim))
                and _listing_after_count(claim[declared.end() :])
            ):
                continue
        count, area, total = _claim_fields(claim)
        if allocation is not None:
            if count != 1:
                raise RejectedCard("unconfirmed_room_unit")
            allocated = True
        if count is not None:
            counts.add(count)
        if area is not None:
            areas.add(area)
        if total is not None:
            totals.add(total)

    if share:
        if not allocated:
            raise RejectedCard("unconfirmed_room_unit")
        counts.add(1)
    if len(counts) > 1:
        raise RejectedCard("conflicting_room_count")
    count = next(iter(counts), None)
    comparable = totals if count is not None and count > 1 else areas
    if len(comparable) > 1:
        raise RejectedCard("conflicting_room_area")
    area = next(iter(comparable), None)
    if area is not None:
        area = decimal_2(area, minimum=Decimal("0.01"), maximum=Decimal("1e10"))
    return count, area
