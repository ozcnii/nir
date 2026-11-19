"""Parse published address strings into components used for matching."""

import re
import unicodedata
from itertools import pairwise

from app.catalog.address import STREET_TYPES, Address, PartialAddress

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
    "комн": "комната",
    "комната": "комната",
    "комната дома": "комната дома",
}
_ROOM_TAGS = {"комната", "комната дома"}
_DETAIL_MARKERS = sorted(_TAGS, key=len, reverse=True)
_TAG = re.compile(rf"({'|'.join(_DETAIL_MARKERS)})\.?\s*(?:№\s*)?({_NUMBER}|[а-я])")
_DISTRICTS = {"район", "р-н"}
_SETTLEMENT_TYPES = {"город", "село", "поселок", "деревня", "станица", "хутор", "аул"}
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
    "территория",
    "поселение",
}
_HOUSE_MARKER = r"(?:дом|д\.)\s*(?:№\s*)?"
_HOUSE = re.compile(rf"(?<!\S)({_HOUSE_MARKER})?({_NUMBER})\Z")
_EXPLICIT_HOUSE = re.compile(rf"(?<![\w/-]){_HOUSE_MARKER}({_NUMBER})(?![\w/-])")
_ADDRESS_TOKEN = re.compile(r"[а-я][а-я0-9 .’'-]*")
_ADDRESS_FORMAT = str.maketrans("ё–—", "е--")


def _plain(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFC", value).lower().translate(_ADDRESS_FORMAT).split()
    )


def _component(value: str) -> str:
    head, space, tail = value.partition(" ")
    return _PREFIXES.get(head, head) + space + tail


def _street_type(word: str) -> str | None:
    kind = _PREFIXES.get(word, word).strip(".")
    return kind if kind in STREET_TYPES else None


def canonical_street(street: str) -> str:
    name, _, suffix = street.rpartition(" ")
    kind = _street_type(suffix)
    return f"{kind} {name}" if name and kind else street


def _address_role(component: str, *, room: bool = False) -> str:
    words = component.split()
    if not words:
        return "other"
    terms = {word.strip(".") for word in words}
    if room and "участок" in terms:
        return "parcel"
    ends = {words[0].strip("."), words[-1].strip(".")}
    if words[:2] == ["жилой", "комплекс"]:
        return "territory"
    if ends & _SETTLEMENT_TYPES:
        return "settlement"
    if _street_type(words[0]) or _street_type(words[-1]):
        return "street"
    if ends & _DISTRICTS:
        return "district"
    if terms & (_TERRITORIES | _DISTRICTS):
        return "territory"
    if ends.intersection((*_TAGS.values(), "дом")):
        return "other"
    return "name"


def _detail_name(part: str) -> str | None:
    for tag in _DETAIL_MARKERS:
        if part.startswith(tag) and not part[len(tag) : len(tag) + 1].isalpha():
            return _TAGS[tag]
    return None


def _address_details(
    parts: tuple[str, ...], normalized: list[str], *, room: bool
) -> tuple[int, dict[str, tuple[str, str]], bool]:
    start = len(parts)
    details: dict[str, tuple[str, str]] = {}
    unresolved: set[str] = set()
    sections: dict[str, set[int]] = {}
    section = 0
    complete = True
    for i, part in enumerate(normalized):
        match = _TAG.fullmatch(part)
        if match is None:
            name = _detail_name(part) if room else None
            number = None
        else:
            name, number = _TAGS[match[1]], match[2]
        if name is None and start == len(parts):
            continue
        start = min(start, i)
        if name is None:
            section += 1
            continue
        previous = details.get(name)
        if (
            number is None
            or name in unresolved
            or (name in _ROOM_TAGS and not room)
            or (name not in ("корпус", "строение") and not number[0].isdigit())
            or (previous is not None and (not room or previous[0] != number))
        ):
            details.pop(name, None)
            unresolved.add(name)
            section += 1
        else:
            # Repeated evidence preserves the value, but cannot complete a chain.
            complete = complete and previous is None
            details.setdefault(name, (number, parts[i]))
            sections.setdefault(name, set()).add(section)

    if room:
        # An interruption cannot confirm that the room belongs to an earlier apartment.
        if (
            sections.get("квартира", set()).isdisjoint(sections.get("комната", set()))
            or "квартира" not in details
        ):
            details.pop("комната", None)
        seen = unresolved | sections.keys()
        if "комната дома" in seen and seen.intersection(("квартира", "комната")):
            details.pop("комната дома", None)
            section += 1
    return start, details, complete and section == 0


def _building_component(component: str) -> tuple[str, str, bool] | None:
    match = _HOUSE.search(component)
    if match is None:
        return None
    return component[: match.start()].rstrip(), match[2], match[1] is not None


def _room_building(component: str) -> tuple[str, str | None, bool, bool] | None:
    matches = list(_EXPLICIT_HOUSE.finditer(component))
    if not matches:
        building = _building_component(component)
        return (*building, True) if building is not None else None
    first = matches[0]
    prefix = component[: first.start()].rstrip()
    if any(
        component[left.end() : right.start()].strip(" ;")
        for left, right in pairwise(matches)
    ):
        # Equal numbers cannot corroborate one house across intervening context.
        return "", None, True, False
    known = matches[-1].end() == len(component) and all(
        match[1] == first[1] for match in matches
    )
    return prefix, first[1] if known else None, True, known and len(matches) == 1


def _settlement_name(component: str, role: str) -> tuple[str, str | None] | None:
    if not _ADDRESS_TOKEN.fullmatch(component):
        return None
    if role != "settlement":
        return component, None
    words = component.split()
    index = 0 if words[0] in _SETTLEMENT_TYPES else -1
    kind = words.pop(index)
    if not words or set(words) & _SETTLEMENT_TYPES:
        return None
    return " ".join(words), kind


def _district_name(component: str) -> str | None:
    if not _ADDRESS_TOKEN.fullmatch(component):
        return None
    words = component.split()
    name = words[1:] if words[0].strip(".") in _DISTRICTS else words[:-1]
    return " ".join(name) or None


def _typed_street(component: str, role: str) -> str | None:
    if role != "street" or not _ADDRESS_TOKEN.fullmatch(component):
        return None
    words = component.split()
    if len(words) < 2 or set(words) & _TERRITORIES:
        return None
    return canonical_street(component)


def _unique[T](choices: set[T]) -> T | None:
    return next(iter(choices)) if len(choices) == 1 else None


def _room_address(
    parts: tuple[str, ...],
    first: int,
    components: list[str],
    details: dict[str, tuple[str, str]],
    *,
    detail_offset: int,
    complete_details: bool,
) -> Address | PartialAddress:
    streets: dict[int, str] = {}
    localities: list[tuple[int, str, str, tuple[str, str | None] | None]] = []
    districts: dict[int, str] = {}
    numbers: list[tuple[int, str, str]] = []
    late_numbers: set[str] = set()
    ambiguous_number = False
    strict_building = True
    labels: list[str] = []
    roles: list[str] = []
    for i, label in enumerate(components):
        role = _address_role(label, room=True)
        building = _room_building(label)
        if i >= detail_offset:
            # A late house number can confirm the core for a repeat, but it
            # cannot make that chain eligible for matching across sources.
            if building is not None and building[2] and role != "parcel":
                if building[1] is None:
                    ambiguous_number = True
                else:
                    late_numbers.add(building[1])
            continue
        street = _typed_street(label, role)
        if building is not None:
            prefix, found_number, explicit, complete_building = building
            prefix_role = _address_role(prefix, room=True)
            inline_street = _typed_street(prefix, prefix_role)
            scope = None
            if role == "parcel" or prefix_role == "parcel":
                role, street = "parcel", None
            elif found_number is None:
                ambiguous_number = True
                label, role, street = prefix, prefix_role, inline_street
                if not prefix:
                    continue
            elif explicit:
                scope = "explicit"
            elif inline_street is not None or (not prefix and i - 1 in streets):
                scope = "street"
            elif not prefix and numbers and numbers[-1][0] == i - 1:
                # A second number in the same position makes the first ambiguous.
                scope = "full"
            elif i == detail_offset - 1 and (
                prefix_role == "name" or (not prefix and roles and roles[-1] == "name")
            ):
                scope = "full"

            if scope is not None and found_number is not None:
                strict_building = strict_building and complete_building
                numbers.append((i, found_number, scope))
                if not prefix:
                    continue
                label, role, street = prefix, prefix_role, inline_street
            elif not prefix:
                role = "other"

        labels.append(label)
        roles.append(role)
        if street is not None:
            streets[i] = street
        if role in ("settlement", "name"):
            localities.append((i, label, role, _settlement_name(label, role)))
        if role == "district" and (district := _district_name(label)) is not None:
            districts[i] = district

    # A sole name in the street position can be compared even if the full
    # chain is invalid. Without a typed place, require the simple address order.
    if not streets and numbers and len(localities) >= 2:
        index, label, _, last = localities[-1]
        place = _unique({known for _, _, _, known in localities[:-1]})
        if (
            last == (label, None)
            and place is not None
            and numbers[0][0] - index in (0, 1)
            and (
                place[1] is not None
                or all(role in ("name", "district") for role in roles)
            )
        ):
            streets[index] = canonical_street(label)
            localities.pop()

    number = None
    street = _unique(set(streets.values()))
    possible_numbers = {found for _, found, _ in numbers} | late_numbers
    if ambiguous_number or len(possible_numbers) > 1:
        details = {}
    elif numbers:
        index, found, _ = numbers[0]
        if (
            strict_building
            and len(numbers) == 1
            and index == detail_offset - 1
            and complete_details
            and not late_numbers
        ):
            full = _complete_address(parts, first, labels, roles, found, details)
            if full is not None:
                return full
        if found in late_numbers or any(
            scope == "explicit"
            or (street is not None and (index in streets or index - 1 in streets))
            for index, _, scope in numbers
        ):
            number = found

    settlement = kind = enclosing_district = city_district = None
    explicit_localities = [entry for entry in localities if entry[2] == "settlement"]
    if explicit_localities and explicit_localities[0] == localities[0]:
        localities = explicit_localities
    known_place = _unique({known for _, _, _, known in localities})
    if known_place is not None:
        first_place, last_place = localities[0][0], localities[-1][0]
        # An untyped settlement needs a resolved street later in the chain.
        if known_place[1] is not None or (
            street is not None and last_place < min(streets)
        ):
            settlement, kind = known_place
            enclosing_district = _unique(
                {name for index, name in districts.items() if index < first_place}
            )
            city_district = _unique(
                {name for index, name in districts.items() if index > last_place}
            )
    return PartialAddress(
        settlement=settlement,
        settlement_kind=kind,
        enclosing_district=enclosing_district,
        district=city_district,
        street=street,
        number=number,
        details=details,
    )


def parse_address(
    value: str | None, region: str, *, room: bool = False
) -> Address | None:
    parsed = _read_address(value, region, room=room)
    return parsed if isinstance(parsed, Address) else None


def read_room_address(
    value: str | None, region: str | None
) -> Address | PartialAddress | None:
    """Read explicit numbers even if the geographic address is incomplete."""
    return _read_address(value, region, room=True)


def _read_address(
    value: str | None, region: str | None, *, room: bool
) -> Address | PartialAddress | None:
    if not value:
        return None
    parts = tuple(item.strip() for item in value.split(","))
    if not room and not all(parts):
        return None
    normalized = [_component(_plain(item)) for item in parts]
    detail_start, details, complete_details = _address_details(
        parts, normalized, room=room
    )
    first = int(region is not None and normalized[0] == _plain(region))
    if room:
        return _room_address(
            parts,
            first,
            normalized[first:],
            details,
            detail_offset=detail_start - first,
            complete_details=complete_details and region is not None,
        )
    components = normalized[first:detail_start]
    if not complete_details or region is None or not components:
        return None
    building = _building_component(components[-1])
    if building is None or any(
        part is not None and part[2]
        for part in map(_building_component, components[:-1])
    ):
        return None
    inline_street, number, _ = building
    complete_components = components[:-1]
    if inline_street:
        complete_components.append(inline_street)
    roles = [_address_role(part) for part in complete_components]
    return _complete_address(parts, first, complete_components, roles, number, details)


def _complete_address(
    parts: tuple[str, ...],
    first: int,
    components: list[str],
    roles: list[str],
    number: str,
    details: dict[str, tuple[str, str]],
) -> Address | None:
    if len(components) < 2 or not all(
        _ADDRESS_TOKEN.fullmatch(part) for part in components
    ):
        return None
    street = components[-1]
    if roles[-1] not in ("street", "name"):
        return None
    locality, locality_roles = components[:-1], roles[:-1]
    settlements = [
        i for i, role in enumerate(locality_roles) if role in ("settlement", "name")
    ]
    districts = [i for i, role in enumerate(locality_roles) if role == "district"]
    if (
        len(settlements) != 1
        or len(districts) > 1
        or any(
            role not in ("settlement", "name", "district") for role in locality_roles
        )
    ):
        return None
    settlement_index = settlements[0]
    named = _settlement_name(
        locality[settlement_index], locality_roles[settlement_index]
    )
    if named is None:
        return None
    locality_name, kind = named
    settlement: tuple[str, ...] = (locality_name,)
    district = district_text = None
    if districts:
        index = districts[0]
        name = _district_name(locality[index])
        if name is None:
            return None
        if index > settlement_index:
            # An explicitly displayed city district may be omitted on one site.
            district, district_text = name, parts[first + index]
        else:
            settlement = (f"район {name}", *settlement)
    return Address(
        settlement,
        canonical_street(street),
        district,
        number,
        details,
        kind,
        first + settlement_index,
        parts,
        district_text,
    )


def mentions_unit(address: str) -> bool:
    return any(
        _TAGS[match[1]] not in _ROOM_TAGS
        for part in address.split(",")
        if (match := _TAG.fullmatch(_plain(part))) is not None
    )
