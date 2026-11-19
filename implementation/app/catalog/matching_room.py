"""Match rooms only when both snapshots identify the same physical room."""

from collections.abc import Iterable

from app.catalog.address import (
    Address,
    address_conflicts,
    address_patch,
    details_conflict,
    same_house,
)
from app.catalog.address_parser import parse_address, published_room_details
from app.catalog.contracts import ActiveSnapshot, PropertyFacts
from app.catalog.matching import Candidate, Match, property_patch


def _unit(details: dict[str, tuple[str, str]]) -> tuple[str | None, str] | None:
    apartment = details.get("квартира")
    room = details.get("комната")
    standalone = details.get("комната дома")
    if standalone is not None:
        return (None, standalone[0]) if apartment is None and room is None else None
    return (
        (apartment[0], room[0]) if apartment is not None and room is not None else None
    )


def _room_address_patch(previous: Address, incoming: Address) -> str | None:
    """Keep a published chain when the next snapshot lacks known qualifiers."""
    if (
        _unit(incoming.details) is None
        or not previous.details.keys() <= incoming.details.keys()
        or details_conflict(previous, incoming)
        or (previous.district is not None and incoming.district is None)
        or (previous.settlement_kind is not None and incoming.settlement_kind is None)
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
    if incoming["rooms"] != 1 or unit is None:
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


def repeated_room_patch(
    saved: PropertyFacts, incoming: ActiveSnapshot, region: str | None
) -> dict[str, object] | None:
    old_count, new_count = saved["rooms"], incoming["rooms"]
    if old_count is not None and new_count is not None and old_count != new_count:
        return None
    address = incoming["address"] if saved["address"] is None else None
    old = parse_address(saved["address"], region, room=True) if region else None
    new = parse_address(incoming["address"], region, room=True) if region else None
    old_details = old.details if old else published_room_details(saved["address"])
    new_details = new.details if new else published_room_details(incoming["address"])
    old_unit, new_unit = _unit(old_details), _unit(new_details)
    if old_unit is not None and new_unit is not None and old_unit != new_unit:
        return None
    if old is not None and new is not None:
        different_scope = any(
            one.detail("квартира") is not None
            and other.detail("комната дома") is not None
            for one, other in ((old, new), (new, old))
        )
        ignore = "комната" if old_unit is None or new_unit is None else None
        if different_scope or address_conflicts(old, new, ignore=ignore):
            return None
        address = _room_address_patch(old, new)
    return property_patch(saved, incoming, address)
