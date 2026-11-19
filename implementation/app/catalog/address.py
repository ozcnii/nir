"""Compare and enrich parsed property addresses."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HouseAddress:
    location: tuple[str, ...]
    settlements: dict[str, str]
    areas: dict[str, str]
    street: str | None
    qualifiers: dict[str, str]
    complete: bool
    block_marked: bool


@dataclass(frozen=True)
class Address:
    locality: tuple[str, ...]
    street: str
    district: str | None
    number: str
    details: dict[str, tuple[str, str]]
    settlement_kind: str | None
    settlement_index: int
    parts: tuple[str, ...]
    district_text: str | None

    @property
    def building_parts(self) -> tuple[str | None, str | None]:
        return self.detail("корпус"), self.detail("строение")

    def detail(self, name: str) -> str | None:
        item = self.details.get(name)
        return item[0] if item is not None else None

    @property
    def location(self) -> tuple[tuple[str, ...], str, str]:
        return (self.locality, self.street, self.number)


def _location_conflicts(left: Address, right: Address) -> bool:
    return left.location != right.location or any(
        first is not None and second is not None and first != second
        for first, second in (
            (left.district, right.district),
            (left.settlement_kind, right.settlement_kind),
        )
    )


def same_house(left: Address, right: Address) -> bool:
    return left.building_parts == right.building_parts and not _location_conflicts(
        left, right
    )


def details_conflict(previous: Address, incoming: Address) -> bool:
    return any(
        previous.details[name][0] != incoming.details[name][0]
        for name in previous.details.keys() & incoming.details.keys()
    )


def apartment_address_conflicts(previous: Address, incoming: Address) -> bool:
    return _location_conflicts(previous, incoming) or details_conflict(
        previous, incoming
    )


def address_patch(previous: Address, incoming: Address) -> str | None:
    updated = list(previous.parts)
    index = previous.settlement_index
    if previous.settlement_kind is None and incoming.settlement_kind is not None:
        updated[index] = incoming.parts[incoming.settlement_index]
    if previous.district is None and incoming.district_text is not None:
        updated.insert(index + 1, incoming.district_text)
    updated.extend(
        value[1]
        for key, value in incoming.details.items()
        if key not in previous.details
    )
    return ", ".join(updated) if tuple(updated) != previous.parts else None
