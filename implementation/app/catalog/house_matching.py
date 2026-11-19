"""Decide house identity using the published address and physical facts."""

from collections.abc import Iterable

from app.catalog.address import HouseAddress
from app.catalog.address_parser import (
    HOUSE_BLOCK_TAGS,
    HOUSE_BUILDING_TAGS,
    parse_house_address,
)
from app.catalog.contracts import PropertyFacts
from app.catalog.matching import Candidate, Match, property_patch


def _conflicting_facts[Role, Value](
    left: dict[Role, Value], right: dict[Role, Value]
) -> bool:
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
        and not _conflicting_facts(left.qualifiers, right.qualifiers)
    )


def has_confirmed_block(
    incoming: HouseAddress | None, previous: HouseAddress | None = None
) -> bool:
    """Require block evidence for a marked address or a previously saved block."""
    previous_block = previous is not None and previous.block_marked
    if incoming is None or not incoming.block_marked:
        return not previous_block
    if incoming.complete and not previous_block:
        return True
    if incoming.chain == "partial" or previous is None or not previous.complete:
        return False
    shared_roles = {
        role for role, _ in previous.qualifiers.items() & incoming.qualifiers.items()
    }
    return bool(shared_roles & HOUSE_BLOCK_TAGS) and bool(
        shared_roles & HOUSE_BUILDING_TAGS
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
    old: HouseAddress | None,
    new: HouseAddress | None,
) -> dict[str, object] | None:
    if new is not None and new.subject_conflict:
        return None
    updated_address = incoming["address"] if previous["address"] is None else None
    if old is not None and new is not None:
        old_block = HOUSE_BLOCK_TAGS.intersection(old.qualifiers)
        new_block = HOUSE_BLOCK_TAGS.intersection(new.qualifiers)
        if (
            _location_conflicts(old, new)
            or _conflicting_facts(_building_parts(old), _building_parts(new))
            or (new_block and old_block != new_block and (old_block or old.complete))
        ):
            return None
        updated_address = _enriched_address(old, new, incoming)
    return property_patch(previous, incoming, updated_address)


def _location_conflicts(old: HouseAddress, new: HouseAddress) -> bool:
    """Only disagreement on established location facts contradicts a repeat."""
    return (
        (old.street is not None and new.street is not None and old.street != new.street)
        or _conflicting_facts(old.settlements, new.settlements)
        or _conflicting_facts(old.areas, new.areas)
        or _leading_settlement_conflicts(old, new)
        or _conflicting_facts(
            old.settlements_by_neighbors, new.settlements_by_neighbors
        )
    )


def _leading_settlement_conflicts(old: HouseAddress, new: HouseAddress) -> bool:
    """Compare leading city names with a common address landmark."""
    return (
        old.leading_settlement is not None
        and new.leading_settlement is not None
        and old.leading_settlement != new.leading_settlement
        and (
            (old.street is not None and old.street == new.street)
            or bool(old.areas.items() & new.areas.items())
        )
    )
