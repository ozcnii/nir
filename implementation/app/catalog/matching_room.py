"""Match rooms only when both snapshots identify the same physical room."""

from collections.abc import Iterable

from app.catalog.address import (
    Address,
    PartialAddress,
    address_patch,
    details_conflict,
    location_conflicts,
    retains_location,
    same_house,
)
from app.catalog.address_parser import parse_address, read_room_address
from app.catalog.contracts import ActiveSnapshot, PropertyFacts
from app.catalog.matching import Candidate, Match, property_patch

ROOM_DETAILS = ("квартира", "комната", "комната дома")


def _unit(
    details: dict[str, tuple[str, str]],
) -> tuple[str | None, str | None] | None:
    apartment = details.get("квартира")
    room = details.get("комната")
    standalone = details.get("комната дома")
    if standalone is not None:
        return (None, standalone[0]) if apartment is None and room is None else None
    if apartment is None:
        return None
    return apartment[0], room[0] if room is not None else None


def _room_address_patch(previous: Address, incoming: Address) -> str | None:
    """Keep a published chain when the next snapshot lacks known qualifiers."""
    unit = _unit(incoming.details)
    if (
        unit is None
        or unit[1] is None
        or not previous.details.keys() <= incoming.details.keys()
        or details_conflict(previous, incoming)
        or not retains_location(previous, incoming)
    ):
        return None
    return ", ".join(incoming.parts) if address_patch(previous, incoming) else None


def _compatible_facts(incoming: ActiveSnapshot, saved: PropertyFacts) -> bool:
    area, floor = incoming["area_m2"], incoming["floor"]
    if not (
        (area is not None and area == saved["area_m2"])
        or (floor is not None and floor == saved["floor"])
    ):
        return False
    return all(
        left is None or right is None or left == right
        for left, right in (
            (area, saved["area_m2"]),
            (floor, saved["floor"]),
            (incoming["floors_total"], saved["floors_total"]),
        )
    )


def choose_room(
    incoming: ActiveSnapshot,
    address: Address,
    candidates: Iterable[Candidate],
    region: str,
) -> Match | None:
    unit = _unit(address.details)
    if incoming["rooms"] != 1 or unit is None or unit[1] is None:
        return None
    choices = []
    for candidate in candidates:
        facts = candidate.facts
        if facts["rooms"] != 1 or not _compatible_facts(incoming, facts):
            continue
        other = parse_address(facts["address"], region, room=True)
        if (
            other is not None
            and _unit(other.details) == unit
            and same_house(other, address)
            and not details_conflict(other, address)
        ):
            choices.append((candidate, other))
    if len(choices) != 1:
        return None
    candidate, other = choices[0]
    return Match(
        candidate.property_id,
        property_patch(
            candidate.facts,
            incoming,
            _room_address_patch(other, address),
        ),
    )


def _conflicting_numbers(
    previous: Address | PartialAddress, incoming: Address | PartialAddress
) -> bool:
    if details_conflict(previous, incoming, ignore=ROOM_DETAILS):
        return True
    old_unit, new_unit = _unit(previous.details), _unit(incoming.details)
    if old_unit is None or new_unit is None:
        return False
    old_scope, old_room = old_unit
    new_scope, new_room = new_unit
    return old_scope != new_scope or (
        old_room is not None and new_room is not None and old_room != new_room
    )


def repeated_room_patch(
    saved: PropertyFacts, incoming: PropertyFacts, region: str | None
) -> dict[str, object] | None:
    old_count, new_count = saved["rooms"], incoming["rooms"]
    if old_count is not None and new_count is not None and old_count != new_count:
        return None
    address = incoming["address"] if saved["address"] is None else None
    old = read_room_address(saved["address"], region)
    new = read_room_address(incoming["address"], region)
    if old is not None and new is not None:
        if location_conflicts(old, new) or _conflicting_numbers(old, new):
            return None
        if isinstance(old, Address) and isinstance(new, Address):
            address = _room_address_patch(old, new)
    return property_patch(saved, incoming, address)
