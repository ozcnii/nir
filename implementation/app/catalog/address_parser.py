"""Parse published address strings into components used for matching."""

import re
import unicodedata

from app.catalog.address import Address, PartialAddress

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
_TAG = re.compile(rf"({'|'.join(_TAGS)})\.?\s*(?:№\s*)?({_NUMBER}|[а-я])")
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
_HOUSE = re.compile(rf"(?<!\S)(?:(дом|д\.)\s*(?:№\s*)?)?({_NUMBER})\Z")
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
    return kind if kind in _STREET_TYPES else None


def canonical_street(street: str) -> str:
    name, _, suffix = street.rpartition(" ")
    kind = _street_type(suffix)
    return f"{kind} {name}" if name and kind else street


def _address_role(component: str) -> str:
    words = component.split()
    terms = {word.strip(".") for word in words}
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


def _address_details(
    parts: tuple[str, ...], normalized: list[str], *, room: bool
) -> tuple[int, dict[str, tuple[str, str]]] | None:
    start = len(parts)
    details: dict[str, tuple[str, str]] = {}
    for i, part in enumerate(normalized):
        match = _TAG.fullmatch(part)
        if match is None:
            if details:
                return None
            continue
        name, number = _TAGS[match[1]], match[2]
        if (
            name in details
            or (name in _ROOM_TAGS and not room)
            or (name not in ("корпус", "строение") and not number[0].isdigit())
        ):
            return None
        details[name] = number, parts[i]
        start = min(start, i)
    return start, details


def _building_component(component: str) -> tuple[str, str, bool] | None:
    match = _HOUSE.search(component)
    if match is None:
        return None
    return component[: match.start()].rstrip(), match[2], match[1] is not None


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


def _typed_street(component: str, role: str) -> str | None:
    if role != "street" or not _ADDRESS_TOKEN.fullmatch(component):
        return None
    words = component.split()
    if len(words) < 2 or set(words) & (_TERRITORIES | {"участок"}):
        return None
    return canonical_street(component)


def _partial_address(
    components: list[str],
    roles: list[str],
    buildings: list[tuple[str, str, bool] | None],
    details: dict[str, tuple[str, str]],
) -> PartialAddress:
    streets: dict[int, str] = {}
    localities: list[tuple[int, str, str]] = []
    numbers: list[tuple[str, bool]] = []
    for i, (label, role, building) in enumerate(
        zip(components, roles, buildings, strict=True)
    ):
        if building is not None:
            prefix, number, explicit = building
            prefix_role = _address_role(prefix) if prefix else ""
            if (
                explicit
                or _typed_street(prefix, prefix_role)
                or (not prefix and i - 1 in streets)
            ):
                numbers.append((number, explicit))
                label, role = prefix, prefix_role
            elif not prefix:
                continue
        street = _typed_street(label, role)
        if street is not None:
            streets[i] = street
        if role in ("settlement", "name"):
            localities.append((i, label, role))

    street_index = next(iter(streets)) if len(streets) == 1 else None
    settlement = kind = None
    if len(localities) == 1:
        i, label, role = localities[0]
        # An untyped name is evidence only in the locality position before a typed street.
        if role == "settlement" or (street_index is not None and i < street_index):
            settlement, kind = _settlement_name(label, role) or (None, None)

    building_number = None
    if len(numbers) == 1:
        number, explicit = numbers[0]
        if explicit or street_index is not None:
            building_number = number
    elif numbers:
        details = {}
    return PartialAddress(
        settlement,
        kind,
        streets.get(street_index) if street_index is not None else None,
        building_number,
        details,
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
    if not all(parts):
        return None
    normalized = [_component(_plain(item)) for item in parts]
    parsed = _address_details(parts, normalized, room=room)
    if parsed is None:
        return None
    detail_start, details = parsed
    first = int(region is not None and normalized[0] == _plain(region))
    components = normalized[first:detail_start]
    roles = [_address_role(part) for part in components]
    buildings = [_building_component(part) for part in components]
    terminal_building = buildings[-1] if buildings else None
    if (
        region is not None
        and terminal_building is not None
        and not any(part is not None and part[2] for part in buildings[:-1])
    ):
        inline_street, number, _ = terminal_building
        complete_components, complete_roles = components[:-1], roles[:-1]
        if inline_street:
            complete_components.append(inline_street)
            complete_roles.append(_address_role(inline_street))
        address = _complete_address(
            parts, first, complete_components, complete_roles, number, details
        )
        if address is not None:
            return address
    return _partial_address(components, roles, buildings, details) if room else None


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
        label = locality[index].split()
        if label[0].strip(".") in _DISTRICTS:
            name = " ".join(label[1:])
        else:
            name = " ".join(label[:-1])
        if not name:
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
