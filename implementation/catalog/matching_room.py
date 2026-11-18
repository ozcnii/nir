"""Match rooms only when both snapshots identify the same physical room."""

from catalog.contracts import ActiveSnapshot
from catalog.matching import Candidate, Match, PropertyFacts, property_patch
from catalog.matching_address import (
    Address,
    address_conflicts,
    address_patch,
    details_conflict,
    parse_address,
    same_house,
)


def _unit(address: Address) -> tuple[str | None, str] | None:
    apartment = address.detail("квартира")
    room = address.detail("комната")
    standalone = address.detail("комната дома")
    if standalone is not None:
        return (None, standalone) if apartment is None and room is None else None
    return (apartment, room) if apartment is not None and room is not None else None


def _room_address_patch(
    stored: str | None, previous: Address, incoming: Address
) -> str | None:
    """Keep a published chain when the next snapshot lacks known qualifiers."""
    if (
        _unit(incoming) is None
        or not previous.details.keys() <= incoming.details.keys()
        or details_conflict(previous, incoming)
        or (previous.district is not None and incoming.district is None)
        or (previous.settlement_kind is not None and incoming.settlement_kind is None)
    ):
        return None
    return address_patch(previous, incoming, stored)


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
    incoming: ActiveSnapshot, address: Address, candidates: list[Candidate], region: str
) -> Match | None:
    unit = _unit(address)
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
            and _unit(other) == unit
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
            _room_address_patch(candidate.facts["address"], other, address),
        ),
    )


def repeated_room_patch(
    saved: PropertyFacts, incoming: ActiveSnapshot, region: str | None
) -> PropertyFacts | None:
    old_count, new_count = saved["rooms"], incoming["rooms"]
    if old_count is not None and new_count is not None and old_count != new_count:
        return None
    address = incoming["address"] if saved["address"] is None else None
    old = parse_address(saved["address"], region, room=True) if region else None
    new = parse_address(incoming["address"], region, room=True) if region else None
    if old is not None and new is not None:
        old_unit, new_unit = _unit(old), _unit(new)
        different_scope = (
            old.detail("квартира") is not None
            and new.detail("комната дома") is not None
        ) or (
            old.detail("комната дома") is not None
            and new.detail("квартира") is not None
        )
        ignore = "комната" if old_unit is None or new_unit is None else None
        if different_scope or address_conflicts(old, new, ignore=ignore):
            return None
        if old_unit is not None and new_unit is not None and old_unit != new_unit:
            return None
        address = _room_address_patch(saved["address"], old, new)
    return property_patch(saved, incoming, address)
