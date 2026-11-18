"""Resolve published claims about the offered room or room set."""

import re
from decimal import Decimal

from collection.normalization import RejectedCard, decimal_2, number, optional_integer

_AMOUNT = r"[0-9]+(?:[.,][0-9]+)?"
_UNIT = r"м[²2](?=\s|[.,;!?]|$)"
_ROOM = r"комнат[ау]"
_WORD_COUNTS = {"одна": "1", "одну": "1", "две": "2", "три": "3"}
_OFFER = re.compile(
    r"^(?:прода[её]тся|продаются|продаю|прода[её]м|продам|сда[её]тся|сдаются|сдаю|сда[её]м|сдам) +"
)
_COUNT = re.compile(
    rf"^(?P<count>[1-9][0-9]*|{'|'.join(_WORD_COUNTS)}) +комнат(?:а|у|ы)?\b"
)
_IDENTIFIER = r"[0-9]+[а-я]?(?:[-/][0-9]+[а-я]?)*\b"
_PAUSE = r" *[,—–-] *"
_ROOM_FRAGMENT = re.compile(
    rf"{_ROOM}\b(?: +№ *(?P<room>{_IDENTIFIER}))?"
    rf"(?:(?: +|{_PAUSE})в квартире +(?:№ *)?{_IDENTIFIER})?"
    rf"(?:(?: +(?:площадью +)?|{_PAUSE}площадью +)(?P<area>{_AMOUNT}) *{_UNIT})?"
)
_ROOM_LINK = re.compile(
    r"(?: *,? +и +|(?P<comma> *, *))(?P<extra>ещ[её] +)?(?:(?:одна|одну|1) +)?"
)
_TOTAL = re.compile(rf" *(?:, *)?общей площадью +({_AMOUNT}) *{_UNIT}")
_SHARE = re.compile(r"^(?:[0-9]+/[0-9]+ +)?дол[яю]\b")
_ALLOCATED = re.compile(
    r"^(?:доля: |(?:[0-9]+/[0-9]+ +)?дол[яю], которо[йю] "
    r"(?:по соглашению )?соответствует )"
    r"(?=(?:комната +№ ?[1-9][0-9]*|(?:одна|1) комната)\b)"
)
_NOT_ALLOCATED = re.compile(r"\b(?:без выделения|не выделен[ао]?)\b")
_OTHER_SCOPE = re.compile(
    r"^[ ,—-]*(?:в соседне[йм]|в друго[йм]|у сосед|не прода|не сда)"
)
_IMAGE = r"на (?:фото|фотографии|плане|схеме)\b"
_DEPICTED = re.compile(rf"^(?:(?: +| *[—–-] *){_IMAGE}| *, *{_IMAGE}(?=$|[,;]))")
_SEPARATE = re.compile(
    r"^[ ,—-]*(?:(?:прода[её]тся|сда[её]тся|можно купить|можно снять) +)?"
    r"(?:отдельно|не входит)\b"
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
        or text.startswith(("комната ", "комната,"))
    )


def _statements(value: str) -> list[str]:
    return _CLAUSE_END.split(_SQUARE_METERS.sub("м²", value))


def _outside_offer(tail: str) -> bool:
    return bool(_OTHER_SCOPE.match(tail) or _DEPICTED.match(tail))


def _enumerated_rooms(claim: str, first: re.Match[str]) -> tuple[int, int]:
    rooms, end = 1, first.end()
    while link := _ROOM_LINK.match(claim, end):
        room = _ROOM_FRAGMENT.match(claim, link.end())
        if room is None:
            break
        if link["comma"] and not (link["extra"] or room["room"] or room["area"]):
            break
        tail = claim[room.end() :]
        if _outside_offer(tail) or _SEPARATE.match(tail):
            break
        rooms, end = rooms + 1, room.end()
    return rooms, end


def _set_area(tail: str) -> Decimal | None:
    total = _TOTAL.match(tail)
    return (
        number(total[1])
        if total is not None and not _outside_offer(tail[total.end() :])
        else None
    )


def _claim_fields(claim: str) -> tuple[int | None, Decimal | None, Decimal | None]:
    count = _COUNT.match(claim)
    if count:
        rooms = optional_integer(_WORD_COUNTS.get(count[1], count[1]), minimum=1)
        tail = claim[count.end() :]
        if _outside_offer(tail):
            return None, None, None
        if rooms != 1:
            return rooms, None, _set_area(tail)
        claim = f"комната{tail}"

    first = _ROOM_FRAGMENT.match(claim)
    if first is None or _outside_offer(claim[first.end() :]):
        return None, None, None
    rooms, end = _enumerated_rooms(claim, first)
    if rooms > 1:
        return rooms, None, _set_area(claim[end:])
    if area := first["area"]:
        return 1, number(area), None
    return (1 if count or first["room"] else None), None, None


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
        elif not offer and index >= len(headings):
            if index != len(headings) or not claim.startswith("уютная "):
                continue
            claim = claim.removeprefix("уютная ")
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
