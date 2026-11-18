"""Conservative apartment matching over plain catalog data."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from catalog.contracts import ActiveSnapshot

FLAT_ID = 1
_NUMBER = r"[0-9]+[а-я]?(?:[-/][0-9]+[а-я]?)*"
_PREFIXES = {
    "г.": "город",
    "с.": "село",
    "пос.": "поселок",
    "ул.": "улица",
    "просп.": "проспект",
    "пр-т": "проспект",
    "пер.": "переулок",
    "бул.": "бульвар",
    "ш.": "шоссе",
    "р-н": "район",
}
_TAGS = {
    "кв": "квартира",
    "квартира": "квартира",
    "корп": "корпус",
    "корпус": "корпус",
    "стр": "строение",
    "строение": "строение",
    "подъезд": "подъезд",
}
_TAG = re.compile(rf"({'|'.join(_TAGS)})\.?\s*(?:№\s*)?({_NUMBER}|[а-я])\Z")
_STREET_TYPES = (
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
_NOT_SETTLEMENT = (
    "район ",
    "жк ",
    "жилой комплекс ",
    "мкр ",
    "микрорайон ",
    "республика ",
    "автономный округ ",
)
_NAME_WITHOUT_STREET = (
    "жк ",
    "жилой комплекс ",
    "корпус ",
    "строение ",
    "квартира ",
    "подъезд ",
    "район ",
    "мкр ",
    "микрорайон ",
)
_HOUSE = re.compile(rf"(?:дом\s*|д\.\s*)?({_NUMBER})\Z")
_STREET_AND_NUMBER = re.compile(
    rf"(?P<street>.+?) +(?:(?:дом|д\.) +)?(?P<num>{_NUMBER})\Z"
)
_ADDRESS_TOKEN = re.compile(r"[а-я][а-я0-9 .’'-]*\Z")
_UNIT_NUMBER = re.compile(r"[0-9]+[а-я]?\Z")
_ROOMS = re.compile(
    r"(?<!\w)(кухня-гостиная|кухня|гостиная|спальня|комната|прихожая|"
    r"коридор|санузел|ванная|гардеробная|кабинет|детская)"
    r"(?:\s*№\s*([1-9][0-9]*))?\s*[:=–-]?\s*"
    r"([0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.?\s*м)\b"
)
_TYPOGRAPHY = str.maketrans(
    {
        "ё": "е",
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "’": "'",
        "‘": "'",
        "–": "-",
        "—": "-",
        "•": " ",
        "●": " ",
        "·": " ",
    }
)
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b50\u2b55\ufe0e\ufe0f\u200d]+"
)


def _plain(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFC", value).lower().replace("ё", "е").split()
    )


def _component(value: str) -> str:
    head, space, tail = value.partition(" ")
    return _PREFIXES.get(head, head) + space + tail


@dataclass(frozen=True)
class Address:
    locality: tuple[str, ...]
    street: str
    district: str | None
    number: str
    details: dict[str, tuple[str, str]]

    @property
    def house(self) -> tuple[object, ...]:
        return (
            self.locality,
            self.street,
            self.number,
            self.detail("корпус"),
            self.detail("строение"),
        )

    def detail(self, name: str) -> str | None:
        item = self.details.get(name)
        return item[0] if item is not None else None

    def base(self) -> tuple[object, ...]:
        return (self.locality, self.street, self.number)


def parse_address(value: str | None, region: str) -> Address | None:
    if not value:
        return None
    parts = [item.strip() for item in value.split(",")]
    normalized = [_component(_plain(item)) for item in parts]
    if not all(normalized):
        return None
    first = int(normalized[0] == _plain(region))
    if len(parts) - first < 2:
        return None
    detail_start = len(parts)
    details: dict[str, tuple[str, str]] = {}
    for index, part in enumerate(normalized[first:], first):
        match = _TAG.fullmatch(part)
        if match:
            name, number = _TAGS[match[1]], match[2]
            if name in details or (
                name in ("квартира", "подъезд") and not _UNIT_NUMBER.fullmatch(number)
            ):
                return None
            details[name] = number, parts[index]
            detail_start = min(detail_start, index)
        elif detail_start != len(parts):
            return None

    core = normalized[first:detail_start]
    if len(core) < 2:
        return None
    house = _HOUSE.fullmatch(core[-1])
    if house is not None:
        if len(core) < 3:
            return None
        locality, street, number = core[:-2], core[-2], house[1]
    else:
        house_and_street = _STREET_AND_NUMBER.fullmatch(core[-1])
        if house_and_street is None:
            return None
        locality = core[:-1]
        street, number = house_and_street["street"], house_and_street["num"]

    if not all(_ADDRESS_TOKEN.fullmatch(part) for part in (*locality, street)):
        return None
    if locality[0].startswith(_NOT_SETTLEMENT) or locality[0].endswith(
        (" область", " край", " республика", " округ")
    ):
        return None
    if street.startswith(_NAME_WITHOUT_STREET):
        return None
    # An explicitly attributed district after the settlement describes a
    # location within it. Either listing can omit it.
    districts = [part for part in locality[1:] if part.startswith("район ")]
    if len(districts) > 1:
        return None
    settlement = tuple(part for part in locality if part not in districts)
    tail = street.rsplit(" ", 1)
    if len(tail) == 2 and tail[1] in _STREET_TYPES:
        street = f"{tail[1]} {tail[0]}"
    return Address(
        settlement, street, districts[0] if districts else None, number, details
    )


class PropertyFacts(TypedDict):
    address: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    area_m2: Decimal | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    land_area_m2: Decimal | None


@dataclass
class Candidate:
    property_id: int
    facts: PropertyFacts
    descriptions: list[str]


@dataclass(frozen=True)
class Match:
    property_id: int
    patch: PropertyFacts


def _seller(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFC", value).lower().translate(_TYPOGRAPHY)
    return " ".join(_EMOJI.sub(" ", normalized).split())


def _has_layout(text: str) -> bool:
    return (
        len(
            {
                (part, ordinal)
                for part, ordinal, size in _ROOMS.findall(text)
                if Decimal(size.replace(",", ".")) > 0
            }
        )
        >= 2
    )


def apartment_address_conflicts(previous: Address, incoming: Address) -> bool:
    district_conflict = (
        previous.district is not None
        and incoming.district is not None
        and previous.district != incoming.district
    )
    return (
        district_conflict
        or previous.base() != incoming.base()
        or any(
            previous.details[name][0] != incoming.details[name][0]
            for name in previous.details.keys() & incoming.details.keys()
        )
    )


def _address_patch(
    previous: Address, incoming: Address, address: str | None
) -> str | None:
    if address is None:
        return None
    extra = incoming.details.keys() - previous.details.keys()
    if not extra:
        return None
    return ", ".join(
        (
            address,
            *(incoming.details[name][1] for name in incoming.details if name in extra),
        )
    )


def _patch(
    previous: PropertyFacts, incoming: PropertyFacts, address: str | None
) -> PropertyFacts:
    floor = previous["floor"] if previous["floor"] is not None else incoming["floor"]
    total = (
        previous["floors_total"]
        if previous["floors_total"] is not None
        else incoming["floors_total"]
    )
    floors_compatible = floor is None or total is None or floor <= total
    return {
        "address": address,
        "latitude": incoming["latitude"] if previous["latitude"] is None else None,
        "longitude": incoming["longitude"] if previous["longitude"] is None else None,
        "area_m2": incoming["area_m2"] if previous["area_m2"] is None else None,
        "rooms": incoming["rooms"] if previous["rooms"] is None else None,
        "floor": incoming["floor"]
        if floors_compatible and previous["floor"] is None
        else None,
        "floors_total": incoming["floors_total"]
        if floors_compatible and previous["floors_total"] is None
        else None,
        "land_area_m2": incoming["land_area_m2"]
        if previous["land_area_m2"] is None
        else None,
    }


def repeated_flat_patch(
    previous: PropertyFacts, incoming: ActiveSnapshot, region: str
) -> PropertyFacts | None:
    """Patch the same property only when the new address is compatible."""
    old = parse_address(previous["address"], region)
    new = parse_address(incoming["address"], region)
    if old is not None and new is not None and apartment_address_conflicts(old, new):
        return None
    address = (
        _address_patch(old, new, previous["address"])
        if old is not None and new is not None
        else incoming["address"]
        if previous["address"] is None
        else None
    )
    return _patch(previous, incoming, address)


def choose_flat(
    incoming: ActiveSnapshot, candidates: list[Candidate], region: str
) -> Match | None:
    address = parse_address(incoming["address"], region)
    if address is None:
        return None
    seller = _seller(incoming["description"])
    detailed = _has_layout(seller)
    unit = address.detail("квартира")
    seen_units = {unit} if unit is not None else set()
    choices: list[tuple[Match, bool]] = []
    for candidate in candidates:
        other = parse_address(candidate.facts["address"], region)
        if other is None or address.house != other.house:
            continue
        other_unit = other.detail("квартира")
        same_text = detailed and any(
            _seller(text) == seller for text in candidate.descriptions
        )
        if same_text and other_unit is not None:
            seen_units.add(other_unit)
        if apartment_address_conflicts(address, other):
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
            patch = _patch(
                candidate.facts,
                incoming,
                _address_patch(other, address, candidate.facts["address"]),
            )
            choices.append((Match(candidate.property_id, patch), by_number))

    valid = [item for item, by_number in choices if by_number or len(seen_units) < 2]
    return valid[0] if len(valid) == 1 else None
