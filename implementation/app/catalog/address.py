"""Compare and enrich parsed property addresses."""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

type LocationFact = tuple[str, str]
type SettlementsByNeighbors = dict[tuple[LocationFact, LocationFact], LocationFact]


@dataclass(frozen=True)
class HouseAddress:
    location: tuple[str, ...]
    settlements: dict[str, str]
    areas: dict[str, str]
    leading_settlement: str | None
    settlements_by_neighbors: SettlementsByNeighbors
    street: str | None
    qualifiers: dict[str, str]
    chain: Literal["partial", "redundant", "complete"]
    block_marked: bool
    subject_conflict: bool

    @property
    def complete(self) -> bool:
        return self.chain == "complete"


STREET_TYPES = (
    "улица",
    "проспект",
    "переулок",
    "бульвар",
    "шоссе",
    "набережная",
    "проезд",
    "площадь",
    "тракт",
)


@dataclass(frozen=True)
class PartialAddress:
    settlement: str | None
    settlement_kind: str | None
    enclosing_district: str | None
    district: str | None
    street: str | None
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
    def settlement(self) -> str:
        return self.locality[-1]

    @property
    def enclosing_district(self) -> str | None:
        if len(self.locality) > 1:
            return self.locality[0].removeprefix("район ")
        return None


def _geography_pairs(
    left: Address | PartialAddress, right: Address | PartialAddress
) -> tuple[tuple[str | None, str | None], ...]:
    return (
        (left.settlement, right.settlement),
        (left.settlement_kind, right.settlement_kind),
        (left.enclosing_district, right.enclosing_district),
        (left.district, right.district),
        (left.number, right.number),
    )


def _geography_conflicts(left: Address, right: Address) -> bool:
    return any(
        first is not None and second is not None and first != second
        for first, second in _geography_pairs(left, right)
    )


def _street_parts(street: str | None) -> tuple[str | None, str | None]:
    if street is None:
        return None, None
    kind, _, name = street.partition(" ")
    return (kind, name) if name and kind in STREET_TYPES else (None, street)


def _location_pairs(
    left: Address | PartialAddress, right: Address | PartialAddress
) -> tuple[tuple[str | None, str | None], ...]:
    left_kind, left_name = _street_parts(left.street)
    right_kind, right_name = _street_parts(right.street)
    return (
        *_geography_pairs(left, right),
        (left_name, right_name),
        (left_kind, right_kind),
    )


def location_conflicts(
    left: Address | PartialAddress, right: Address | PartialAddress
) -> bool:
    return any(
        first is not None and second is not None and first != second
        for first, second in _location_pairs(left, right)
    )


def retains_location(previous: Address, incoming: Address) -> bool:
    return all(
        old is None or old == new for old, new in _location_pairs(previous, incoming)
    )


def same_house(left: Address, right: Address) -> bool:
    return (
        left.locality == right.locality
        and left.street == right.street
        and left.building_parts == right.building_parts
        and not _geography_conflicts(left, right)
    )


def details_conflict(
    previous: Address | PartialAddress,
    incoming: Address | PartialAddress,
    *,
    ignore: Collection[str] = (),
) -> bool:
    return any(
        previous.details[name][0] != incoming.details[name][0]
        for name in previous.details.keys() & incoming.details.keys()
        if name not in ignore
    )


def address_conflicts(previous: Address, incoming: Address) -> bool:
    return (
        previous.locality != incoming.locality
        or previous.street != incoming.street
        or _geography_conflicts(previous, incoming)
        or details_conflict(previous, incoming)
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
