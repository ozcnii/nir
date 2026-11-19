"""Parse published address strings into components used for matching."""

import re
import unicodedata

from app.catalog.address import Address, HouseAddress

_NUMBER = r"[0-9]+[а-я]?(?:[-/][0-9]+[а-я]?)*"
_HOUSE_NUMBER = re.compile(_NUMBER)
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
HOUSE_BLOCK_TAGS = {"блок", "секция", "таунхаус"}
_HOUSE_BLOCK = re.compile(
    rf"(?<!\S)({'|'.join(sorted(HOUSE_BLOCK_TAGS))})(?=$|[\s.№0-9])"
)
_HOUSE_QUALIFIERS = {"корпус", "строение", "участок", *HOUSE_BLOCK_TAGS}
_HOUSE_MARKERS = {"д": "дом", "корп": "корпус", "стр": "строение"}
_HOUSE_TERRITORIES = ("снт ", "сдт ", "кп ", "территория ", "коттеджный поселок ")


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


def _building_component(component: str) -> tuple[str, str, bool] | None:
    match = _HOUSE.search(component)
    if match is None:
        return None
    return component[: match.start()].rstrip(), match[2], match[1] is not None


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
                name in ("квартира", "подъезд") and not number[0].isdigit()
            ):
                return None
            details[name] = number, parts[index]
            detail_start = min(detail_start, index)
        elif detail_start != len(parts):
            return None

    core = normalized[first:detail_start]
    if len(core) < 2:
        return None
    house = _building_component(core[-1])
    if house is None:
        return None
    inline_street, number, _ = house
    if inline_street:
        locality, street = core[:-1], inline_street
    elif len(core) >= 3:
        locality, street = core[:-2], core[-2]
    else:
        return None

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
    settlement_index = settlements[0]
    named = _settlement_name(locality[settlement_index], roles[settlement_index])
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
        tuple(parts),
        district_text,
    )


def mentions_unit(address: str) -> bool:
    return any(_TAG.fullmatch(_plain(part)) for part in address.split(","))


def _house_marker(component: str) -> tuple[str, str] | None:
    token, _, identifier = component.replace(".", " ", 1).partition(" ")
    label = _HOUSE_MARKERS.get(token, token)
    if label == "дом" or label in _HOUSE_QUALIFIERS:
        return label, identifier.strip().removeprefix("№").lstrip()
    return None


def _valid_house_identifier(label: str, identifier: str) -> bool:
    return bool(_HOUSE_NUMBER.fullmatch(identifier)) or (
        label not in ("дом", "участок")
        and len(identifier) == 1
        and identifier.isalpha()
    )


def _house_components(parts: list[str]) -> list[str]:
    components: list[str] = []
    for part in parts:
        for match in _HOUSE_BLOCK.finditer(part):
            prefix = part[: match.start()].strip()
            if not prefix or _building_component(prefix) is not None:
                if prefix:
                    components.append(prefix)
                detail = part[match.end() :].removeprefix(".").lstrip()
                components.append(f"{match[1]} {detail}")
                break
        else:
            components.append(part)
    return components


def _house_settlements(
    location: list[str], street: str | None
) -> tuple[dict[str, str], bool]:
    typed: dict[str, set[str]] = {}
    untyped: list[str] = []
    after_street = False
    for part in location:
        after_street |= part == street
        if part.startswith(_HOUSE_TERRITORIES):
            continue
        role = _address_role(part)
        if role == "settlement":
            named = _settlement_name(part, role)
            if named is not None:
                name, kind = named
                if kind is not None:
                    typed.setdefault(kind, set()).add(name)
        elif role == "name" and not after_street:
            untyped.append(part)
    if not typed and street is not None and len(untyped) == 1:
        typed[""] = {untyped[0]}
    return (
        {kind: next(iter(names)) for kind, names in typed.items() if len(names) == 1},
        all(len(names) <= 1 for names in typed.values()),
    )


def parse_house_address(value: str | None, region: str) -> HouseAddress | None:
    """Read positive address evidence and retain known parts of incomplete addresses."""
    if not value:
        return None
    parts = [_component(_plain(part)) for part in value.split(",")]
    valid = all(parts)
    parts = [part for part in parts if part]
    if parts[:1] == ["россия"]:
        parts.pop(0)
    if parts:
        first = parts[0]
        if first.endswith(" обл."):
            first = first.removesuffix(" обл.") + " область"
        if first == _plain(region) and _address_role(first) == "territory":
            parts.pop(0)
    location: list[str] = []
    numbers: set[str] = set()
    identifiers: dict[str, set[str]] = {}
    block_marked = False
    labeled_building = False
    for part in _house_components(parts):
        marker = _house_marker(part)
        if marker is not None and marker[0] != "дом":
            label, identifier = marker
            block_marked |= label in HOUSE_BLOCK_TAGS
            ids = identifiers.setdefault(label, set())
            known = _valid_house_identifier(label, identifier)
            if ids or not known:
                valid = False
            if known:
                ids.add(identifier)
            continue
        building = _building_component(part)
        if building is not None:
            prefix, published_number, explicit = building
            is_inline_street = bool(prefix and _address_role(prefix) == "street")
            follows_street = bool(location and _address_role(location[-1]) == "street")
            if (
                explicit
                or is_inline_street
                or (not prefix and follows_street and not identifiers)
            ):
                valid &= not numbers and (not prefix or is_inline_street)
                numbers.add(published_number)
                labeled_building |= explicit
                if is_inline_street:
                    location.append(canonical_street(prefix))
                continue
        if marker is not None or _HOUSE_NUMBER.fullmatch(part) or part.startswith("№"):
            valid = False
            continue
        if numbers or identifiers:
            valid = False
        location.append(canonical_street(part))

    qualifiers = {
        tag: next(iter(ids)) for tag, ids in identifiers.items() if len(ids) == 1
    }
    number = next(iter(numbers)) if len(numbers) == 1 else None
    if not numbers:
        number = qualifiers.get("строение") or qualifiers.get("таунхаус")
        labeled_building = number is not None
    streets = [part for part in location if _address_role(part) == "street"]
    street = streets[0] if len(streets) == 1 and " " in streets[0] else None
    settlements, consistent = _house_settlements(location, street)
    territory = any(part.startswith(_HOUSE_TERRITORIES) for part in location)
    has_street = (
        street is not None
        and location[-1] == street
        and (territory or bool(settlements))
    )
    rural = any(kind not in ("", "город") for kind in settlements)
    complete = (
        valid
        and consistent
        and len(streets) <= 1
        and len(HOUSE_BLOCK_TAGS.intersection(qualifiers)) <= 1
        and number is not None
        and (has_street or (labeled_building and (territory or rural)))
    )
    return HouseAddress(
        location=tuple(location),
        settlements=settlements,
        street=street,
        number=number,
        qualifiers=qualifiers,
        complete=complete,
        block_marked=block_marked,
    )
