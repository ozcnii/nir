"""Conservative apartment matching over plain catalog data."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from typing import TypedDict

from catalog.contracts import ActiveSnapshot
from catalog.matching_address import (
    Address,
    address_patch,
    apartment_address_conflicts,
    details_conflict,
    mentions_unit,
    parse_address,
    same_house,
)

FLAT_ID = 1
_LAYOUT_ITEM = re.compile(
    r"(?<!\w)(?P<room>кухня-гостиная|кухня|гостиная|спальня|комната|прихожая|"
    r"коридор|санузел|ванная|гардеробная|кабинет|детская)"
    r"(?:\s*№\s*(?P<ordinal>[1-9][0-9]*))?(?:-\s*|\s*(?:[:=]\s*|-\s+)?)"
    r"(?P<area>[0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.?\s*м)\b"
    r"|\b(?:без комиссии|возможна ипотека|перепланировка не предусмотрена"
    r"|не предусмотрена перепланировка)\b"
)
_LIST_SEPARATOR = re.compile(r"\s*(?:[,;]\s*(?:и\s+)?|и\s+)|\s+")
_FOREIGN_HEADING = re.compile(
    r"(?:(?:на )?фото |планировка )?соседн\w*\s+квартир\w*\b"
    r"\s*[,;:-]?\s*(?:а\s+)?"
)
_OTHER_UNIT = re.compile(
    r"\b(?:соседн\w*|друг\w*|аналогичн\w*|типов\w*)\s+(?:квартир\w*|кв\b)"
    r"|\b(?:у|планировка|квартира)\s+соседей\b"
    r"|\b(?:нескольк\w*|разн\w*|друг\w*|типов\w*|вариант\w*)\s+планиров"
)
_OFFERED_UNIT = re.compile(
    r"(?:в (?:продаваемой|предлагаемой|сдаваемой) квартире"
    r"|планировка (?:продаваемой|предлагаемой|сдаваемой) квартиры)\b\s*[:–-]?\s*"
)
_LAYOUT_HEADING = re.compile(r"(?:планировка:|в (?:этой )?квартире\b)\s*")
_BACK_REFERENCE = re.compile(
    r"(?:(?:эти|указанные|названные|это)\s+(?:размеры|площади|помещения|комнаты)"
    r"|оба|обе)\b\s*"
)
_DENIAL = re.compile(r"\b(?:отсутству\w*|не\s+(?:предусмотрен\w*|относят\w*))\b")
_SENTENCE_END = re.compile(r"[.!?](?![0-9])")
_SQUARE_METERS = re.compile(r"\bкв\.\s*м\b")
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
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b50\u2b55\ufe0e\ufe0f\u200d]+"
)


class PropertyFacts(TypedDict):
    address: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    area_m2: Decimal | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    land_area_m2: Decimal | None


@dataclass
class Candidate:
    property_id: int
    facts: PropertyFacts
    descriptions: list[str]


@dataclass(frozen=True)
class Match:
    property_id: int
    patch: PropertyFacts


def _seller(value: str | None) -> str:
    normalized = unicodedata.normalize("NFC", value or "").lower()
    return " ".join(_EMOJI.sub(" ", normalized.translate(_TYPOGRAPHY)).split())


def _layout_assertions(
    text: str,
) -> list[tuple[tuple[str, str | None], Decimal]] | None:
    measurements: list[tuple[tuple[str, str | None], Decimal]] = []
    end = 0
    for item in _LAYOUT_ITEM.finditer(text):
        gap = text[end : item.start()]
        if (end and not _LIST_SEPARATOR.fullmatch(gap)) or (not end and gap.strip()):
            return None
        end = item.end()
        room = item["room"]
        if room is not None:
            area = Decimal(item["area"].replace(",", "."))
            if area <= 0:
                return None
            measurements.append(((room, item["ordinal"]), area))
    return measurements if not text[end:].strip() else None


def _has_layout(text: str) -> bool:
    confirmed: list[tuple[tuple[str, str | None], Decimal]] = []
    pending: list[tuple[tuple[str, str | None], Decimal]] = []
    on_offer = True
    for sentence in _SENTENCE_END.split(_SQUARE_METERS.sub("м²", text)):
        sentence = sentence.strip()
        if reference := _BACK_REFERENCE.match(sentence):
            claim = sentence[reference.end() :]
            if _OTHER_UNIT.search(claim) or _DENIAL.search(claim):
                pending.clear()

        if heading := _FOREIGN_HEADING.match(sentence):
            confirmed.extend(pending)
            pending.clear()
            on_offer = False
            sentence = sentence[heading.end() :]
        if _OTHER_UNIT.search(sentence):
            on_offer = False
            continue
        if heading := _OFFERED_UNIT.match(sentence):
            confirmed.extend(pending)
            pending.clear()
            on_offer = True
            sentence = sentence[heading.end() :]
        if on_offer:
            claims = _LAYOUT_HEADING.sub("", sentence, count=1)
            pending.extend(_layout_assertions(claims) or ())

    rooms: dict[tuple[str, str | None], set[Decimal]] = {}
    for room, area in (*confirmed, *pending):
        rooms.setdefault(room, set()).add(area)
    combined = {ordinal for kind, ordinal in rooms if kind == "кухня-гостиная"}
    if combined:
        for kind, ordinal in tuple(rooms):
            if kind in ("кухня", "гостиная") and (
                ordinal is None or None in combined or ordinal in combined
            ):
                del rooms[kind, ordinal]
    numbered_kinds = {kind for kind, ordinal in rooms if ordinal is not None}
    return (
        sum(
            len(areas) == 1 and (ordinal is not None or kind not in numbered_kinds)
            for (kind, ordinal), areas in rooms.items()
        )
        >= 2
    )


def _patch(
    previous: PropertyFacts, incoming: PropertyFacts, address: str | None
) -> PropertyFacts:
    floor = previous["floor"] if previous["floor"] is not None else incoming["floor"]
    total = (
        previous["floors_total"]
        if previous["floors_total"] is not None
        else incoming["floors_total"]
    )
    floors_compatible = floor is None or total is None or floor <= total
    return {
        "address": address,
        "latitude": incoming["latitude"] if previous["latitude"] is None else None,
        "longitude": incoming["longitude"] if previous["longitude"] is None else None,
        "area_m2": incoming["area_m2"] if previous["area_m2"] is None else None,
        "rooms": incoming["rooms"] if previous["rooms"] is None else None,
        "floor": incoming["floor"]
        if floors_compatible and previous["floor"] is None
        else None,
        "floors_total": incoming["floors_total"]
        if floors_compatible and previous["floors_total"] is None
        else None,
        "land_area_m2": incoming["land_area_m2"]
        if previous["land_area_m2"] is None
        else None,
    }


def repeated_flat_patch(
    previous: PropertyFacts, incoming: ActiveSnapshot, region: str
) -> PropertyFacts | None:
    """Patch the same property only when the new address is compatible."""
    old = parse_address(previous["address"], region)
    new = parse_address(incoming["address"], region)
    if (
        old is None
        and previous["address"] is not None
        and incoming["address"] is not None
    ):
        # The current address may hold stronger evidence than the parser can resolve.
        return _patch(previous, previous, None)
    if new is None and incoming["address"] is not None:
        if mentions_unit(incoming["address"]):
            # A unit qualifier in an unparseable chain cannot establish compatibility.
            return None
        return _patch(previous, previous, None)
    if old is not None and new is not None and apartment_address_conflicts(old, new):
        return None
    address = (
        address_patch(old, new, previous["address"])
        if old is not None and new is not None
        else incoming["address"]
        if previous["address"] is None
        else None
    )
    return _patch(previous, incoming, address)


def _is_template(house: Address, numbered: list[tuple[Address, str]]) -> bool:
    in_house = [
        (address, unit) for address, unit in numbered if same_house(house, address)
    ]
    return any(
        first_unit != second_unit and same_house(first, second)
        for (first, first_unit), (second, second_unit) in combinations(in_house, 2)
    )


def choose_flat(
    incoming: ActiveSnapshot, address: Address, candidates: list[Candidate], region: str
) -> Match | None:
    seller = _seller(incoming["description"])
    detailed = _has_layout(seller)
    unit = address.detail("квартира")
    numbered: list[tuple[Address, str]] = []
    if unit is not None:
        numbered.append((address, unit))
    choices: list[tuple[Candidate, Address, bool]] = []
    for candidate in candidates:
        other = parse_address(candidate.facts["address"], region)
        if other is None or not same_house(address, other):
            continue
        other_unit = other.detail("квартира")
        same_text = detailed and any(
            _seller(text) == seller for text in candidate.descriptions
        )
        if same_text and other_unit is not None:
            numbered.append((other, other_unit))
        if details_conflict(address, other):
            continue
        common = [
            left == right
            for left, right in (
                (incoming["area_m2"], candidate.facts["area_m2"]),
                (incoming["rooms"], candidate.facts["rooms"]),
                (incoming["floor"], candidate.facts["floor"]),
            )
            if left is not None and right is not None
        ]
        if not common or not all(common):
            continue
        by_number = unit is not None and other_unit is not None
        if by_number or (
            same_text
            and incoming["area_m2"] is not None
            and incoming["area_m2"] == candidate.facts["area_m2"]
            and incoming["floor"] is not None
            and incoming["floor"] == candidate.facts["floor"]
        ):
            choices.append((candidate, other, by_number))

    valid = [
        (candidate, house)
        for candidate, house, by_number in choices
        if by_number or not _is_template(house, numbered)
    ]
    if len(valid) != 1:
        return None
    candidate, house = valid[0]
    return Match(
        candidate.property_id,
        _patch(
            candidate.facts,
            incoming,
            address_patch(house, address, candidate.facts["address"]),
        ),
    )
