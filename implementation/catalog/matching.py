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
    mentions_unit,
    parse_address,
    same_house,
)

FLAT_ID = 1
_ROOMS = re.compile(
    r"(?<!\w)(кухня-гостиная|кухня|гостиная|спальня|комната|прихожая|"
    r"коридор|санузел|ванная|гардеробная|кабинет|детская)"
    r"(?:\s*№\s*([1-9][0-9]*))?\s*(?:[:=]\s*|-\s+)?"
    r"([0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.?\s*м)\b"
)
_OTHER_UNIT = re.compile(
    r"\b(?:соседн\w*|друг\w*|аналогичн\w*|типов\w*)\s+(?:квартир\w*|кв\b)"
    r"|\b(?:у|планировка|квартира)\s+соседей\b"
    r"|\b(?:нескольк\w*|разн\w*|друг\w*|типов\w*|вариант\w*)\s+планиров"
)
_OFFERED_UNIT = re.compile(r"\b(?:продаваем\w*|предлагаем\w*|сдаваем\w*)\s+квартир\w*")
_UNASSERTED = re.compile(
    r"\b(?:не\s+(?:предусмотр\w*|выдел\w*|буд\w*|име\w*|существ\w*)"
    r"|возможно|отсутству\w*"
    r"|(?:можно|планируется)\s+(?:сделать|выделить|устроить)\w*"
    r"|нет\s+(?:(?:таких|указанных)\s+)?помещени\w*)\b"
)
_SENTENCE_END = re.compile(
    rf"[.!?](?![0-9])|\n(?=[^\n]*(?:{_OTHER_UNIT.pattern}|{_OFFERED_UNIT.pattern}))"
)
_CLAUSE_END = re.compile(r",(?![0-9])|;")
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
    return _EMOJI.sub(" ", normalized.translate(_TYPOGRAPHY))


def _has_layout(text: str) -> bool:
    rooms: set[tuple[str, str]] = set()
    on_offer = True
    for sentence in _SENTENCE_END.split(_SQUARE_METERS.sub("м²", text)):
        denied = _UNASSERTED.search(sentence)
        for clause in _CLAUSE_END.split(sentence):
            other = _OTHER_UNIT.search(clause)
            if other or _OFFERED_UNIT.search(clause):
                on_offer = not other
            if on_offer and not denied:
                rooms.update(
                    (name, ordinal)
                    for name, ordinal, size in _ROOMS.findall(clause)
                    if Decimal(size.replace(",", ".")) > 0
                )

    if ("кухня-гостиная", "") in rooms:
        rooms.difference_update({("кухня", ""), ("гостиная", "")})
    return len(rooms) >= 2


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


def choose_flat(
    incoming: ActiveSnapshot, candidates: list[Candidate], region: str
) -> Match | None:
    address = parse_address(incoming["address"], region)
    if address is None:
        return None
    seller = _seller(incoming["description"])
    description = " ".join(seller.split())
    detailed = _has_layout(seller)
    unit = address.detail("квартира")
    numbered: list[tuple[Address, str]] = []
    if unit is not None:
        numbered.append((address, unit))
    choices: list[tuple[Match, bool]] = []
    for candidate in candidates:
        other = parse_address(candidate.facts["address"], region)
        if other is None or not same_house(address, other):
            continue
        other_unit = other.detail("квартира")
        equal_texts = (
            [
                formatted
                for formatted in map(_seller, candidate.descriptions)
                if " ".join(formatted.split()) == description
            ]
            if detailed
            else []
        )
        if equal_texts and other_unit is not None:
            numbered.append((other, other_unit))
        if apartment_address_conflicts(address, other):
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
            any(map(_has_layout, equal_texts))
            and incoming["area_m2"] is not None
            and incoming["area_m2"] == candidate.facts["area_m2"]
            and incoming["floor"] is not None
            and incoming["floor"] == candidate.facts["floor"]
        ):
            patch = _patch(
                candidate.facts,
                incoming,
                address_patch(other, address, candidate.facts["address"]),
            )
            choices.append((Match(candidate.property_id, patch), by_number))

    template = any(
        first_unit != second_unit and same_house(first, second)
        for (first, first_unit), (second, second_unit) in combinations(numbered, 2)
    )
    valid = [item for item, by_number in choices if by_number or not template]
    return valid[0] if len(valid) == 1 else None
