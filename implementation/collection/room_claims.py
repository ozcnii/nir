"""Resolve published claims about the offered room or room set."""

import re
from decimal import Decimal

from collection.normalization import RejectedCard, decimal_2, number, optional_integer

_AMOUNT = r"[0-9]+(?:[.,][0-9]+)?"
_UNIT = r"м[²2](?=\s|[.,;!?]|$)"
_ROOM = r"комнат[ау]"
_WORD_COUNTS = {"одна": "1", "одну": "1", "две": "2", "три": "3"}
_OFFER = re.compile(r"^(?:прода[её]тся|продаются|продаю|сда[её]тся|сдаются|сдаю) +")
_COUNT = re.compile(
    rf"^(?P<count>[1-9][0-9]*|{'|'.join(_WORD_COUNTS)}) +комнат(?:а|у|ы)?\b"
)
_SINGLE_AREA = re.compile(rf"^{_ROOM} +(площадью +)?(?P<area>{_AMOUNT}) *{_UNIT}")
_PAIRED = re.compile(rf"^{_ROOM} +{_AMOUNT} *{_UNIT} +и +{_ROOM} +{_AMOUNT} *{_UNIT}")
_TOTAL = re.compile(rf"^ +общей площадью +({_AMOUNT}) *{_UNIT}")
_SHARE = re.compile(r"^(?:[0-9]+/[0-9]+ +)?дол[яю]\b")
_ALLOCATED = re.compile(
    r"^(?:доля: (?:(?:одна|1) комната\b|комната № ?[1-9][0-9]*\b)"
    r"|(?:[0-9]+/[0-9]+ +)?дол[яю], которо[йю] "
    r"(?:по соглашению )?соответствует комната № ?[1-9][0-9]*\b)"
)
_NOT_ALLOCATED = re.compile(r"\b(?:без выделения|не выделен[ао]?|на плане)\b")
_OTHER_SCOPE = re.compile(
    r"^[ ,—-]*(?:в соседне[йм]|в друго[йм]|у сосед|не прода|не сда)"
)
_SEPARATE = re.compile(r"\b(?:отдельно|не входит в (?:сделку|продажу|аренду))\b")
_CLAUSE_END = re.compile(r"[.!?;](?:\s+|$)|\n+")
_SQUARE_METERS = re.compile(r"\bкв\.\s+м\b", re.IGNORECASE)


def room_heading(title: str) -> bool:
    """Check the advertised heading independently of the route category."""
    text = _OFFER.sub("", title).removeprefix("доля: ")
    return _COUNT.match(text) is not None or text.startswith(("комната ", "комната,"))


def _claim_fields(claim: str) -> tuple[int | None, Decimal | None, Decimal | None]:
    count = _COUNT.match(claim)
    if count:
        rooms = optional_integer(_WORD_COUNTS.get(count[1], count[1]), minimum=1)
        tail = claim[count.end() :]
        total = _TOTAL.match(tail) if rooms != 1 else None
        if rooms != 1:
            return rooms, None, number(total[1]) if total else None
        claim = f"комната{tail}"
    area = _SINGLE_AREA.match(claim)
    if not area:
        return (1 if count else None), None, None
    if _OTHER_SCOPE.match(claim[area.end() :]):
        return None, None, None
    if _PAIRED.match(claim) and not _SEPARATE.search(claim[area.end() :]):
        return 2, None, None
    return 1, number(area["area"]), None


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
    sources = (title, *(_CLAUSE_END.split(_SQUARE_METERS.sub("м²", description or ""))))
    for index, source in enumerate(sources):
        claim = " ".join(source.casefold().split())
        offer = _OFFER.match(claim)
        if offer:
            claim = claim[offer.end() :].removeprefix("только ")
        if _SHARE.match(claim):
            share = True
            if _NOT_ALLOCATED.search(claim):
                raise RejectedCard("unconfirmed_room_unit")
            if not _ALLOCATED.match(claim):
                continue
            allocated = True
            if not claim.startswith("доля: "):
                continue
            claim = claim.removeprefix("доля: ")
        if index and not offer:
            if index != 1 or not claim.startswith("уютная "):
                continue
            claim = claim.removeprefix("уютная ")
        count, area, total = _claim_fields(claim)
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
