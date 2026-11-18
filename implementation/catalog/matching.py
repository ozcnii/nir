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
_DISTRICTS = {"район", "р-н", "р–н"}
_TERRITORIES = {
    "область",
    "край",
    "республика",
    "округ",
    "муниципалитет",
    "микрорайон",
    "мкр",
    "квартал",
    "жк",
    "снт",
    "кп",
}
_HOUSE = re.compile(rf"(?:дом\s*|д\.\s*)?({_NUMBER})\Z")
_STREET_AND_NUMBER = re.compile(
    rf"(?P<street>.+?) +(?:(?:дом|д\.) +)?(?P<num>{_NUMBER})\Z"
)
_ADDRESS_TOKEN = re.compile(r"[а-я][а-я0-9 .’'-]*\Z")
_UNIT_NUMBER = re.compile(rf"{_NUMBER}\Z")
_ROOMS = re.compile(
    r"(?<!\w)(кухня-гостиная|кухня|гостиная|спальня|комната|прихожая|"
    r"коридор|санузел|ванная|гардеробная|кабинет|детская)"
    r"(?:\s*№\s*([1-9][0-9]*))?\s*[:=–-]?\s*"
    r"([0-9]+(?:[.,][0-9]+)?)\s*(?:м[²2]|кв\.?\s*м)\b"
)
_OTHER_UNIT = re.compile(
    r"\b(?:соседн\w*|друг\w*|аналогичн\w*|типов\w*)\s+(?:квартир\w*|кв\b)"
    r"|\b(?:нескольк\w*|разн\w*|друг\w*|типов\w*|вариант\w*)\s+планиров"
)
_OFFERED_UNIT = re.compile(r"\b(?:продаваем\w*|предлагаем\w*)\s+квартир\w*")
_UNASSERTED = re.compile(r"\b(?:не|нет|без|возможн\w*|планиру\w*|проект\w*)\b")
_CLAUSE_END = re.compile(r"[.!?](?![0-9])|;")
_SQUARE_METERS = re.compile(r"\bкв\.\s*м\b")
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


def _address_role(component: str) -> str:
    words = component.split()
    ends = {words[0].strip("."), words[-1].strip(".")}
    if ends & _DISTRICTS:
        return "district"
    if (
        ends & _TERRITORIES
        or component.startswith("жилой комплекс ")
        or set(words[:2] + words[-2:]).intersection({"район", "округ"})
    ):
        return "territory"
    if ends.intersection(_STREET_TYPES):
        return "street"
    if ends & {"город", "село", "поселок", "деревня", "станица", "хутор", "аул"}:
        return "settlement"
    if not ends.intersection((*_TAGS.values(), "дом", "жилой комплекс")):
        return "name"
    return "other"


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
    roles = [_address_role(part) for part in locality]
    if _address_role(street) not in ("street", "name"):
        return None
    # Only one component may identify the settlement. Districts can qualify it.
    settlements = [i for i, role in enumerate(roles) if role in ("settlement", "name")]
    districts = [i for i, role in enumerate(roles) if role == "district"]
    if (
        len(settlements) != 1
        or len(districts) > 1
        or any(role not in ("settlement", "name", "district") for role in roles)
    ):
        return None
    district = None
    settlement = tuple(locality)
    if districts:
        index = districts[0]
        label = locality[index].split()
        if label[0].strip(".") in _DISTRICTS:
            name = " ".join(label[1:])
        else:
            name = " ".join(label[:-1])
        if not name:
            return None
        if index > settlements[0]:
            # An explicitly displayed city district may be omitted on one site.
            district = name
            settlement = (locality[settlements[0]],)
        else:
            settlement = (f"район {name}", locality[settlements[0]])
    tail = street.rsplit(" ", 1)
    if len(tail) == 2 and tail[1] in _STREET_TYPES:
        street = f"{tail[1]} {tail[0]}"
    return Address(settlement, street, district, number, details)


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
    rooms: set[tuple[str, str]] = set()
    on_offer = True
    for clause in _CLAUSE_END.split(_SQUARE_METERS.sub("м²", text)):
        if _OTHER_UNIT.search(clause):
            on_offer = False
        elif _OFFERED_UNIT.search(clause):
            on_offer = True
        if on_offer and not _UNASSERTED.search(clause):
            rooms.update(
                (name, ordinal)
                for name, ordinal, size in _ROOMS.findall(clause)
                if Decimal(size.replace(",", ".")) > 0
            )

    if ("кухня-гостиная", "") in rooms:
        rooms.difference_update({("кухня", ""), ("гостиная", "")})
    return len(rooms) >= 2


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
    if (
        old is None
        and previous["address"] is not None
        and incoming["address"] is not None
    ):
        # The current address may hold stronger evidence than the parser can resolve.
        return _patch(previous, previous, None)
    if new is None and incoming["address"] is not None:
        if any(_TAG.fullmatch(_plain(part)) for part in incoming["address"].split(",")):
            # A unit qualifier in an unparseable chain cannot establish compatibility.
            return None
        return _patch(previous, previous, None)
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
