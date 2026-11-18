"""Check explicit seller claims about the room offered in a card."""

import re
from decimal import Decimal

from collection.normalization import RejectedCard, number

_ROOM_AREA = re.compile(
    r"(?:^|[.!?]\s+|\n\s*)(?:прода[её]тся|продаю|сда[её]тся|сдаю|уютная)\s+"
    r"комнат[ау]\s+(?:площадью\s+)?([0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.\s+м\.?)"
    r"(?=\s|[.,;!?]|$)(?P<context>[^.!?\n]*)",
    re.IGNORECASE,
)
_OTHER_ROOM = re.compile(
    r"\b(?:в соседней|в другой|у соседей|на фото|на плане|не прода[её]тся|не сда[её]тся)\b",
    re.IGNORECASE,
)
_ABSTRACT_SHARE = re.compile(
    r"^(?:(?:прода[её]тся|продаю)\s+)?(?:[0-9]+/[0-9]+\s+)?дол[яю]\b",
    re.IGNORECASE,
)
_ROOM_SHARE = re.compile(
    r"^доля:\s+(?:(?:одна|1) комната\b|комната № ?[1-9][0-9]*\b)", re.IGNORECASE
)


def validate_room_claims(
    description: str | None, rooms: int | None, area: Decimal | None
) -> None:
    if not description:
        return
    seller = description.lstrip()
    if _ABSTRACT_SHARE.match(seller) and (rooms != 1 or not _ROOM_SHARE.match(seller)):
        raise RejectedCard("unconfirmed_room_unit")
    if (
        rooms == 1
        and area is not None
        and any(
            number(claim[1]) != area and not _OTHER_ROOM.search(claim["context"])
            for claim in _ROOM_AREA.finditer(seller)
        )
    ):
        raise RejectedCard("conflicting_room_area")
