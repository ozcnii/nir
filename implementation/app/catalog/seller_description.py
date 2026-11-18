"""Find usable evidence in the full seller description."""

import re
import unicodedata
from decimal import Decimal

# Overlapping names cannot establish two distinct rooms.
_OVERLAPPING_ROOM_NAMES = {
    "детская": "спальня",
    "ванная": "санузел",
    "прихожая": "коридор",
}
_ROOM_MEASURE = re.compile(
    r"(?P<room>кухня-гостиная|кухня|гостиная|спальня|комната|прихожая|"
    r"коридор|санузел|ванная|гардеробная|кабинет|детская)"
    r"(?:\s*№\s*(?P<ordinal>[1-9][0-9]*))?"
    r"(?:-\s*|\s*(?:[:=]\s*|-\s+)?)"
    r"(?P<area>[0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.?\s*м)\b"
)
_LAYOUT_SEPARATOR = re.compile(r"\s*(?:[,;.]\s*(?:и\s+)?|и\s+)|\s+")
_LAYOUT_INTRODUCTION = re.compile(
    r"^(?:планировка:\s*|в (?:(?:этой|продаваемой|предлагаемой|сдаваемой) )?"
    r"квартире\b\s*:?\s*)"
)

_TYPOGRAPHY = str.maketrans(
    {
        "ё": "е",
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "’": "'",
        "‘": "'",
        "–": "-",
        "—": "-",
        "•": " ",
        "●": " ",
        "·": " ",
    }
)


def normalize_description(value: str | None) -> str:
    normalized = unicodedata.normalize("NFC", value or "").lower()
    tokens = normalized.translate(_TYPOGRAPHY).split()
    # A detached decorative symbol is formatting. Keep symbols within words and №.
    text = " ".join(
        token
        for token in tokens
        if token == "№" or any(unicodedata.category(char) != "So" for char in token)
    )
    return _ROOM_MEASURE.sub(lambda item: item[0].replace(",", "."), text)


def _has_layout(text: str) -> bool:
    """Require a complete list of measurements. Free prose leaves attribution unknown."""
    text = _LAYOUT_INTRODUCTION.sub("", text)

    rooms: dict[tuple[str, str | None], set[Decimal]] = {}
    offset = 0
    while item := _ROOM_MEASURE.match(text, offset):
        area = Decimal(item["area"].replace(",", "."))
        if area <= 0:
            return False
        room, ordinal = item["room"], item["ordinal"]
        if room != "комната" or ordinal is not None:
            kind = _OVERLAPPING_ROOM_NAMES.get(room, room)
            rooms.setdefault((kind, ordinal), set()).add(area)
        offset = item.end()
        if text[offset:] in ("", "!", "."):
            break
        separator = _LAYOUT_SEPARATOR.match(text, offset)
        if separator is None:
            return False
        offset = separator.end()
    else:
        return False

    combined = {ordinal for kind, ordinal in rooms if kind == "кухня-гостиная"}
    if combined:
        for kind, ordinal in tuple(rooms):
            if kind in ("кухня", "гостиная") and (
                ordinal is None or None in combined or ordinal in combined
            ):
                del rooms[kind, ordinal]
    has_specific_names = any(kind != "комната" for kind, _ in rooms)
    numbered_kinds = {kind for kind, ordinal in rooms if ordinal is not None}
    return (
        sum(
            len(areas) == 1
            and (kind != "комната" or not has_specific_names)
            and (ordinal is not None or kind not in numbered_kinds)
            for (kind, ordinal), areas in rooms.items()
        )
        >= 2
    )


def matching_description(value: str | None) -> str | None:
    text = normalize_description(value)
    return text if _has_layout(text) else None
