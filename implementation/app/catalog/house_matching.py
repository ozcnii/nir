"""Decide house identity using the published address and physical facts."""

from collections.abc import Iterable

from app.catalog.address import HouseAddress
from app.catalog.address_parser import HOUSE_BLOCK_TAGS, parse_house_address
from app.catalog.contracts import PropertyFacts
from app.catalog.matching import Candidate, Match, property_patch


def _conflicting_ids(left: dict[str, str], right: dict[str, str]) -> bool:
    return any(left[tag] != right[tag] for tag in left.keys() & right.keys())


def _building_parts(address: HouseAddress) -> dict[str, str]:
    return {
        tag: number for tag, number in address.qualifiers.items() if tag != "участок"
    }


def _same_building(left: HouseAddress, right: HouseAddress) -> bool:
    return (
        left.complete
        and right.complete
        and left.location == right.location
        and _building_parts(left) == _building_parts(right)
        and not _conflicting_ids(left.qualifiers, right.qualifiers)
    )


def _same_house_facts(left: PropertyFacts, right: PropertyFacts) -> bool:
    return (
        left["area_m2"] is not None
        and left["area_m2"] == right["area_m2"]
        and left["floors_total"] is not None
        and left["floors_total"] == right["floors_total"]
        and not any(
            first is not None and second is not None and first != second
            for first, second in (
                (left["rooms"], right["rooms"]),
                (left["land_area_m2"], right["land_area_m2"]),
            )
        )
    )


def choose_house(
    incoming: PropertyFacts,
    address: HouseAddress,
    candidates: Iterable[Candidate],
    region: str,
) -> Match | None:
    match = None
    for candidate in candidates:
        known = candidate.facts
        if not _same_house_facts(incoming, known):
            continue
        other = parse_house_address(known["address"], region)
        if other is None or not _same_building(address, other):
            continue
        if match is not None:
            return None
        match = Match(
            candidate.property_id,
            property_patch(
                known, incoming, _enriched_address(other, address, incoming)
            ),
        )
    return match


def _contains_location(complete: tuple[str, ...], known: tuple[str, ...]) -> bool:
    components = iter(complete)
    return all(part in components for part in known)


def _enriched_address(
    old: HouseAddress, new: HouseAddress, incoming: PropertyFacts
) -> str | None:
    # The fuller chain must come from one publication that retains every known part.
    if (
        new.complete
        and old != new
        and _contains_location(new.location, old.location)
        and old.qualifiers.items() <= new.qualifiers.items()
    ):
        return incoming["address"]
    return None


def repeated_house_patch(
    previous: PropertyFacts,
    incoming: PropertyFacts,
    region: str,
    new: HouseAddress | None,
) -> dict[str, object] | None:
    old = parse_house_address(previous["address"], region)
    updated_address = incoming["address"] if previous["address"] is None else None
    if old is not None and new is not None:
        old_block = HOUSE_BLOCK_TAGS.intersection(old.qualifiers)
        new_block = HOUSE_BLOCK_TAGS.intersection(new.qualifiers)
        if (
            _location_conflicts(old, new)
            or _conflicting_ids(_building_parts(old), _building_parts(new))
            or (new_block and old_block != new_block and (old_block or old.complete))
        ):
            return None
        updated_address = _enriched_address(old, new, incoming)
    return property_patch(previous, incoming, updated_address)


def _location_conflicts(old: HouseAddress, new: HouseAddress) -> bool:
    if (
        (old.street is not None and new.street is not None and old.street != new.street)
        or _conflicting_ids(old.settlements, new.settlements)
        or _conflicting_ids(old.areas, new.areas)
    ):
        return True
    if len(old.settlements) == len(new.settlements) == 1:
        old_kind, old_name = next(iter(old.settlements.items()))
        new_kind, new_name = next(iter(new.settlements.items()))
        if old_name != new_name or (old_kind and new_kind and old_kind != new_kind):
            return True
    return (
        old.complete
        and new.complete
        and not _contains_location(old.location, new.location)
        and not _contains_location(new.location, old.location)
    )
