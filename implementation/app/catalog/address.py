"""Compare and enrich parsed property addresses."""

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class AddressNumbers:
    number: str | None
    details: dict[str, tuple[str, str]]


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


def location_conflicts(left: Address, right: Address) -> bool:
    return left.location != right.location or any(
        first is not None and second is not None and first != second
        for first, second in (
            (left.district, right.district),
            (left.settlement_kind, right.settlement_kind),
        )
    )


def same_house(left: Address, right: Address) -> bool:
    return left.building_parts == right.building_parts and not location_conflicts(
        left, right
    )


def details_conflict(
    previous: Address | AddressNumbers,
    incoming: Address | AddressNumbers,
    *,
    ignore: Collection[str] = (),
) -> bool:
    return any(
        previous.details[name][0] != incoming.details[name][0]
        for name in previous.details.keys() & incoming.details.keys()
        if name not in ignore
    )


def address_conflicts(previous: Address, incoming: Address) -> bool:
    return location_conflicts(previous, incoming) or details_conflict(
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
