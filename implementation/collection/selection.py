"""Collection selection and its accepted source/region/category/deal directions."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import product
from typing import cast

from catalog.reference_data_v1 import SOURCE_CODES

_CATEGORIES = {
    "resale": (1, "flat"),
    "new_build": (1, "flat"),
    "room": (2, "room"),
    "house": (3, "house"),
}


class InvalidSelection(ValueError):
    pass


@dataclass(frozen=True)
class Direction:
    source: str
    region_id: int
    category: str
    deal_type: str


@dataclass(frozen=True)
class Selection:
    sources: tuple[str, ...]
    regions: tuple[int, ...]
    categories: tuple[str, ...]
    deal_types: tuple[str, ...]

    def record(self) -> dict[str, object]:
        return {
            "sources": self.sources,
            "regions": self.regions,
            "categories": self.categories,
            "deal_types": self.deal_types,
        }

    def directions(self) -> Iterator[Direction]:
        for values in product(
            self.sources, self.regions, self.categories, self.deal_types
        ):
            yield Direction(*values)


def parse(value: object) -> Selection:
    if not isinstance(value, dict) or set(value) != {
        "sources",
        "regions",
        "categories",
        "deal_types",
    }:
        raise InvalidSelection("invalid_selection")
    fields = cast(dict[str, object], value)

    def strings(key: str) -> tuple[str, ...]:
        items = fields[key]
        if (
            not isinstance(items, list)
            or not items
            or len(items) > 32
            or not all(isinstance(item, str) and 0 < len(item) <= 32 for item in items)
            or len(set(items)) != len(items)
        ):
            raise InvalidSelection("invalid_selection")
        return tuple(cast(list[str], items))

    places = fields["regions"]
    if (
        not isinstance(places, list)
        or not places
        or len(places) > 100
        or not all(type(place) is int and 0 < place < 2**63 for place in places)
        or len(set(places)) != len(places)
    ):
        raise InvalidSelection("invalid_selection")
    sources = strings("sources")
    categories = strings("categories")
    deals = strings("deal_types")
    if any(source not in SOURCE_CODES for source in sources):
        raise InvalidSelection("unsupported_or_disabled_source")
    if any(category not in _CATEGORIES for category in categories):
        raise InvalidSelection("unsupported_or_missing_property_type")
    if any(deal not in ("sale", "month_rent", "day_rent") for deal in deals):
        raise InvalidSelection("unsupported_deal_type")
    return Selection(sources, tuple(cast(list[int], places)), categories, deals)


def validate(selection: Selection, supported: Callable[[Direction], bool]) -> None:
    from catalog import queries

    if any(queries.source_enabled(code) is not True for code in selection.sources):
        raise InvalidSelection("unsupported_or_disabled_source")
    if any(queries.region_name(region) is None for region in selection.regions):
        raise InvalidSelection("unsupported_or_missing_region")
    for category in selection.categories:
        mapping = _CATEGORIES.get(category)
        if mapping is None or queries.property_kind(mapping[0]) != mapping[1]:
            raise InvalidSelection("unsupported_or_missing_property_type")
    if any(not queries.valid_deal_type(deal) for deal in selection.deal_types):
        raise InvalidSelection("unsupported_deal_type")
    if not all(supported(direction) for direction in selection.directions()):
        raise InvalidSelection("unsupported_direction")
