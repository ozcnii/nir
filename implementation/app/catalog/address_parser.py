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
_HOUSE_MARKERS = {
    "дом": "дом",
    "д.": "дом",
    "корпус": "корпус",
    "корп.": "корпус",
    "корп": "корпус",
    "строение": "строение",
    "стр.": "строение",
    "стр": "строение",
    "земельный участок": "участок",
    "участок": "участок",
    **{tag: tag for tag in HOUSE_BLOCK_TAGS},
}
_HOUSE_MARKER = re.compile(
    rf"(?<!\S)({'|'.join(map(re.escape, _HOUSE_MARKERS))})(?=$|[\s.№0-9])"
)
_HOUSE_TERRITORIES = {
    "снт": "снт",
    "сдт": "сдт",
    "кп": "кп",
    "коттеджный поселок": "кп",
}
_ENCLOSING_DISTRICT = "enclosing district"
_LOCAL_DISTRICT = "local district"
_DISTRICT_SCOPES = {_ENCLOSING_DISTRICT, _LOCAL_DISTRICT}


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


def _district_name(component: str) -> str:
    words = component.split()
    label = words[1:] if words[0].strip(".") in _DISTRICTS else words[:-1]
    return " ".join(label)


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
        name = _district_name(locality[index])
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


def _valid_house_identifier(label: str, identifier: str) -> bool:
    return bool(_HOUSE_NUMBER.fullmatch(identifier)) or (
        label not in ("дом", "участок")
        and len(identifier) == 1
        and identifier.isalpha()
    )


def _house_territory(part: str) -> tuple[str, str] | None:
    name = part.removeprefix("территория ")
    for label, scope in _HOUSE_TERRITORIES.items():
        if name.startswith(f"{label} "):
            return scope, name[len(label) + 1 :]
        if name.endswith(f" {label}"):
            return scope, name[: -len(label) - 1]
    return ("территория", name) if name != part else None


def _house_component(part: str) -> tuple[str, list[tuple[str, str]]]:
    territory = _house_territory(part)
    if territory is not None:
        return " ".join(territory), []
    role = _address_role(part)
    if role == "district":
        district = _district_name(part)
        return f"район {district}" if district else part, []
    if role == "settlement":
        return part, []
    markers = list(_HOUSE_MARKER.finditer(part))
    if not markers:
        return part, []
    labels = []
    ends = [marker.start() for marker in markers[1:]] + [len(part)]
    for marker, end in zip(markers, ends, strict=True):
        identifier = part[marker.end() : end].strip().removeprefix(".").lstrip()
        labels.append((_HOUSE_MARKERS[marker[1]], identifier.removeprefix("№").strip()))
    return part[: markers[0].start()].strip(), labels


def _named_street(part: str) -> str | None:
    if " " in part and _address_role(part) == "street":
        return canonical_street(part)
    return None


def _house_geography(
    location: list[str], street: str | None
) -> tuple[dict[str, str], dict[str, str], bool]:
    typed: dict[str, set[str]] = {}
    areas: dict[str, set[str]] = {}
    untyped: list[str] = []
    after_street = False
    for part in location:
        after_street |= part == street
        territory = _house_territory(part)
        if territory is not None:
            scope, name = territory
            areas.setdefault(scope, set()).add(name)
            continue
        role = _address_role(part)
        if role == "settlement":
            named = _settlement_name(part, role)
            if named is not None:
                name, kind = named
                if kind is not None:
                    typed.setdefault(kind, set()).add(name)
        elif role == "district":
            name = _district_name(part)
            inside_place = typed or untyped or areas.keys() - _DISTRICT_SCOPES
            scope = _LOCAL_DISTRICT if inside_place else _ENCLOSING_DISTRICT
            if name:
                areas.setdefault(scope, set()).add(name)
        elif role == "name" and not after_street:
            untyped.append(part)
    if not typed and street is not None and len(untyped) == 1:
        typed[""] = {untyped[0]}
    return (
        {kind: next(iter(names)) for kind, names in typed.items() if len(names) == 1},
        {kind: next(iter(names)) for kind, names in areas.items() if len(names) == 1},
        all(len(names) <= 1 for names in (*typed.values(), *areas.values())),
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
    numbers: list[str | None] = []
    identifiers: dict[str, set[str]] = {}
    for raw_part in parts:
        part, labels = _house_component(raw_part)
        if part:
            building = _building_component(part)
            if building is None:
                if numbers or identifiers:
                    valid = False
                street = _named_street(part)
                if labels and street is None:
                    valid = False
                location.append(street or part)
            else:
                prefix, published_number, _ = building
                street = _named_street(prefix)
                follows_street = bool(location and _named_street(location[-1]))
                if street is not None or (
                    not prefix and follows_street and not identifiers
                ):
                    if identifiers:
                        valid = False
                    numbers.append(published_number)
                    if street is not None:
                        location.append(street)
                elif prefix:
                    if labels or numbers or identifiers:
                        valid = False
                    location.append(_named_street(part) or part)
                else:
                    valid = False

        for label, identifier in labels:
            if label == "дом":
                numbers.append(
                    identifier if _HOUSE_NUMBER.fullmatch(identifier) else None
                )
                continue
            ids = identifiers.setdefault(label, set())
            known = _valid_house_identifier(label, identifier)
            if ids or not known:
                valid = False
            if known:
                ids.add(identifier)

    qualifiers = {
        tag: next(iter(ids)) for tag, ids in identifiers.items() if len(ids) == 1
    }
    if numbers:
        number = numbers[0] if len(numbers) == 1 else None
    else:
        number = qualifiers.get("строение") or qualifiers.get("таунхаус")
    streets = [part for part in location if _named_street(part) is not None]
    street = streets[0] if len(streets) == 1 else None
    settlements, areas, consistent = _house_geography(location, street)
    blocks = HOUSE_BLOCK_TAGS.intersection(identifiers)
    territory = bool(areas.keys() - _DISTRICT_SCOPES)
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
        and len(blocks) <= 1
        and number is not None
        and (has_street or (territory or rural))
    )
    return HouseAddress(
        location=tuple(location),
        settlements=settlements,
        areas=areas,
        street=street,
        number=number,
        qualifiers=qualifiers,
        complete=complete,
        block_marked=bool(blocks),
    )
