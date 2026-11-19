"""Interpret scoped room numbers and independent facts in incomplete addresses."""

import re
from collections.abc import Iterable
from itertools import pairwise

from app.catalog.address import Address, PartialAddress
from app.catalog.address_parser import (
    ADDRESS_DETAILS,
    ADDRESS_NUMBER,
    ADDRESS_TOKEN,
    address_role,
    building_component,
    canonical_street,
    complete_street_address,
    district_name,
    expand_address_prefix,
    is_address_territory,
    normalize_address_text,
    settlement_name,
)

_TAGS = {
    **ADDRESS_DETAILS,
    "комн": "комната",
    "комната": "комната",
    "комната дома": "комната дома",
}
_ROOM_TAGS = {"комната", "комната дома"}
_DETAIL_MARKERS = sorted(_TAGS, key=len, reverse=True)
_TAG = re.compile(
    rf"({'|'.join(_DETAIL_MARKERS)})\.?\s*(?:№\s*)?({ADDRESS_NUMBER}|[а-я])"
)
_EXPLICIT_HOUSE = re.compile(
    rf"(?<![\w/-])(?:дом|д\.)\s*(?:№\s*)?({ADDRESS_NUMBER})(?![\w/-])"
)


def _room_address_role(component: str) -> str:
    words = component.split()
    if any(word.strip(".") == "участок" for word in words):
        return "parcel"
    role = address_role(component, abbreviations=True)
    if role == "name" and {words[0].strip("."), words[-1].strip(".")} & _ROOM_TAGS:
        return "other"
    return role


def _detail_name(part: str) -> str | None:
    for tag in _DETAIL_MARKERS:
        if part.startswith(tag) and not part[len(tag) : len(tag) + 1].isalpha():
            return _TAGS[tag]
    return None


def _address_details(
    parts: tuple[str, ...], normalized: list[str]
) -> tuple[int, dict[str, tuple[str, str]], bool]:
    start = len(parts)
    details: dict[str, tuple[str, str]] = {}
    unresolved: set[str] = set()
    sections: dict[str, set[int]] = {}
    section = 0
    complete = True
    for i, part in enumerate(normalized):
        match = _TAG.fullmatch(part)
        if match is None:
            name = _detail_name(part)
            number = None
        else:
            name, number = _TAGS[match[1]], match[2]
        if name is None and start == len(parts):
            continue
        start = min(start, i)
        if name is None:
            section += 1
            continue
        previous = details.get(name)
        if (
            number is None
            or name in unresolved
            or (name not in ("корпус", "строение") and not number[0].isdigit())
            or (previous is not None and previous[0] != number)
        ):
            details.pop(name, None)
            unresolved.add(name)
            section += 1
        else:
            # Repeated evidence preserves the value, but cannot complete a chain.
            complete = complete and previous is None
            details.setdefault(name, (number, parts[i]))
            sections.setdefault(name, set()).add(section)

    # An interruption cannot confirm that the room belongs to an earlier apartment.
    if (
        sections.get("квартира", set()).isdisjoint(sections.get("комната", set()))
        or "квартира" not in details
    ):
        details.pop("комната", None)
    seen = unresolved | sections.keys()
    if "комната дома" in seen and seen.intersection(("квартира", "комната")):
        # Neither address scope is established when both are published.
        for name in ("квартира", *_ROOM_TAGS):
            details.pop(name, None)
        section += 1
    return start, details, complete and section == 0


def _room_building(component: str) -> tuple[str, str | None, bool, bool] | None:
    matches = list(_EXPLICIT_HOUSE.finditer(component))
    if not matches:
        building = building_component(component)
        return (*building, True) if building is not None else None
    first = matches[0]
    prefix = component[: first.start()].rstrip()
    if any(
        component[left.end() : right.start()].strip(" ;")
        for left, right in pairwise(matches)
    ):
        # Equal numbers cannot corroborate one house across intervening context.
        return "", None, True, False
    known = matches[-1].end() == len(component) and all(
        match[1] == first[1] for match in matches
    )
    return prefix, first[1] if known else None, True, known and len(matches) == 1


def _typed_street(component: str, role: str) -> str | None:
    if role != "street" or not ADDRESS_TOKEN.fullmatch(component):
        return None
    words = component.split()
    if len(words) < 2 or is_address_territory(words):
        return None
    return canonical_street(component, abbreviations=True)


def _unique[T](choices: set[T]) -> T | None:
    return next(iter(choices)) if len(choices) == 1 else None


def _known_settlement(
    evidence: Iterable[tuple[str, str | None]],
) -> tuple[str, str | None] | None:
    names: set[str] = set()
    kinds: set[str] = set()
    for name, kind in evidence:
        names.add(name)
        if kind is not None:
            kinds.add(kind)
    settlement = _unique(names)
    return (settlement, _unique(kinds)) if settlement is not None else None


def _room_address(
    parts: tuple[str, ...],
    first: int,
    components: list[str],
    details: dict[str, tuple[str, str]],
    *,
    detail_offset: int,
    complete_details: bool,
) -> Address | PartialAddress:
    streets: dict[int, str] = {}
    localities: list[tuple[int, tuple[str, str | None]]] = []
    districts: dict[int, str] = {}
    numbers: list[tuple[int, str, str]] = []
    late_numbers: set[str] = set()
    ambiguous_number = False
    unreadable_locality = False
    strict_building = True
    labels: list[str] = []
    roles: list[str] = []
    for i, label in enumerate(components):
        role = _room_address_role(label)
        building = _room_building(label)
        if i >= detail_offset:
            # A late house number can confirm the core for a repeat, but it
            # cannot make that chain eligible for matching across sources.
            if building is not None and building[2] and role != "parcel":
                if building[1] is None:
                    ambiguous_number = True
                else:
                    late_numbers.add(building[1])
            continue
        street = _typed_street(label, role)
        if building is not None:
            prefix, found_number, explicit, complete_building = building
            prefix_role = _room_address_role(prefix)
            inline_street = _typed_street(prefix, prefix_role)
            scope = None
            if role == "parcel" or prefix_role == "parcel":
                role, street = "parcel", None
            elif found_number is None:
                ambiguous_number = True
                label, role, street = prefix, prefix_role, inline_street
                if not prefix:
                    continue
            elif explicit:
                scope = "explicit"
            elif inline_street is not None or (not prefix and i - 1 in streets):
                scope = "street"
            elif not prefix and numbers and numbers[-1][0] == i - 1:
                # A second number in the same position makes the first ambiguous.
                scope = "full"
            elif i == detail_offset - 1 and (
                prefix_role == "name" or (not prefix and roles and roles[-1] == "name")
            ):
                scope = "full"

            if scope is not None and found_number is not None:
                strict_building = strict_building and complete_building
                numbers.append((i, found_number, scope))
                if not prefix:
                    continue
                label, role, street = prefix, prefix_role, inline_street
            elif not prefix:
                role = "other"

        labels.append(label)
        roles.append(role)
        if street is not None:
            streets[i] = street
        if role in ("settlement", "name"):
            named = settlement_name(label, role)
            if named is None:
                unreadable_locality = True
            else:
                localities.append((i, named))
        if (
            role == "district"
            and ADDRESS_TOKEN.fullmatch(label)
            and (district := district_name(label))
        ):
            districts[i] = district

    # Unreadable fragments do not prove a competing locality, but break the
    # sequence required to infer an unmarked street by position.
    if not unreadable_locality and not streets and numbers and len(localities) >= 2:
        index, (name, kind) = localities[-1]
        place = _known_settlement(known for _, known in localities[:-1])
        if (
            kind is None
            and place is not None
            and name != place[0]
            and numbers[0][0] - index in (0, 1)
            and (
                place[1] is not None
                or all(role in ("name", "district") for role in roles)
            )
        ):
            streets[index] = canonical_street(name, abbreviations=True)
            localities.pop()

    number = None
    street = _unique(set(streets.values()))
    possible_numbers = {found for _, found, _ in numbers} | late_numbers
    if ambiguous_number or len(possible_numbers) > 1:
        details = {}
    elif numbers:
        index, found, _ = numbers[0]
        if (
            strict_building
            and roles
            and (roles[-1] == "street" or street is not None)
            and len(numbers) == 1
            and index == detail_offset - 1
            and complete_details
            and not late_numbers
        ):
            full = complete_street_address(
                parts, first, labels, roles, found, details, abbreviations=True
            )
            if full is not None:
                return full
        if found in late_numbers or any(
            scope == "explicit"
            or (street is not None and (index in streets or index - 1 in streets))
            for index, _, scope in numbers
        ):
            number = found

    settlement = kind = enclosing_district = city_district = None
    # An untyped settlement needs a resolved street later in the chain.
    street_index = min(streets) if street is not None else None
    localities = [
        (index, known)
        for index, known in localities
        if known[1] is not None or (street_index is not None and index < street_index)
    ]
    known_place = _known_settlement(known for _, known in localities)
    if known_place is not None:
        settlement, kind = known_place
        first_place, last_place = localities[0][0], localities[-1][0]
        enclosing_district = _unique(
            {name for index, name in districts.items() if index < first_place}
        )
        city_district = _unique(
            {name for index, name in districts.items() if index > last_place}
        )
    return PartialAddress(
        settlement=settlement,
        settlement_kind=kind,
        enclosing_district=enclosing_district,
        district=city_district,
        street=street,
        number=number,
        details=details,
    )


def parse_room_address(value: str | None, region: str) -> Address | None:
    parsed = read_room_address(value, region)
    return parsed if isinstance(parsed, Address) else None


def read_room_address(
    value: str | None, region: str | None
) -> Address | PartialAddress | None:
    """Read explicit numbers even if the geographic address is incomplete."""
    if not value:
        return None
    parts = tuple(item.strip() for item in value.split(","))
    normalized = [expand_address_prefix(normalize_address_text(item)) for item in parts]
    detail_start, details, complete_details = _address_details(parts, normalized)
    first = int(region is not None and normalized[0] == normalize_address_text(region))
    return _room_address(
        parts,
        first,
        normalized[first:],
        details,
        detail_offset=detail_start - first,
        complete_details=complete_details and region is not None,
    )
