"""Conservative apartment matching over plain catalog data."""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations

from app.catalog.address import (
    Address,
    address_patch,
    apartment_address_conflicts,
    details_conflict,
    same_house,
)
from app.catalog.address_parser import mentions_unit, parse_address
from app.catalog.contracts import ActiveSnapshot, PropertyFacts
from app.catalog.seller_description import matching_description, normalize_description


@dataclass
class Candidate:
    property_id: int
    facts: PropertyFacts
    descriptions: list[str]


@dataclass(frozen=True)
class Match:
    property_id: int
    patch: dict[str, object]


def property_patch(
    previous: PropertyFacts, incoming: PropertyFacts, address: str | None
) -> dict[str, object]:
    patch: dict[str, object] = {
        key: incoming.get(key)
        for key in ("latitude", "longitude", "area_m2", "rooms", "land_area_m2")
        if previous.get(key) is None and incoming.get(key) is not None
    }
    if address is not None:
        patch["address"] = address
    floor = previous["floor"] if previous["floor"] is not None else incoming["floor"]
    total = (
        previous["floors_total"]
        if previous["floors_total"] is not None
        else incoming["floors_total"]
    )
    if floor is None or total is None or floor <= total:
        if previous["floor"] is None and floor is not None:
            patch["floor"] = floor
        if previous["floors_total"] is None and total is not None:
            patch["floors_total"] = total
    return patch


def repeated_flat_patch(
    previous: PropertyFacts, incoming: ActiveSnapshot, region: str
) -> dict[str, object] | None:
    """Patch the same property only when the new address is compatible."""
    old = parse_address(previous["address"], region)
    new = parse_address(incoming["address"], region)
    if (
        old is None
        and previous["address"] is not None
        and incoming["address"] is not None
    ):
        # The current address may hold stronger evidence than the parser can resolve.
        return {}
    if new is None and incoming["address"] is not None:
        if mentions_unit(incoming["address"]):
            # A unit qualifier in an unparseable chain cannot establish compatibility.
            return None
        return {}
    if old is not None and new is not None and apartment_address_conflicts(old, new):
        return None
    address = (
        address_patch(old, new)
        if old is not None and new is not None
        else incoming["address"]
        if previous["address"] is None
        else None
    )
    return property_patch(previous, incoming, address)


def _is_template(house: Address, numbered: list[tuple[Address, str]]) -> bool:
    in_house = [
        (address, unit) for address, unit in numbered if same_house(house, address)
    ]
    return any(
        first_unit != second_unit and same_house(first, second)
        for (first, first_unit), (second, second_unit) in combinations(in_house, 2)
    )


def choose_flat(
    incoming: ActiveSnapshot,
    address: Address,
    candidates: Iterable[Candidate],
    region: str,
) -> Match | None:
    seller = matching_description(incoming["description"])
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
        same_text = seller is not None and any(
            normalize_description(text) == seller for text in candidate.descriptions
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
        property_patch(
            candidate.facts,
            incoming,
            address_patch(house, address),
        ),
    )
