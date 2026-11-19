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
_HOUSE = re.compile(rf"(?:дом\s*|д\.\s*)?({_NUMBER})")
_ADDRESS_TOKEN = re.compile(r"[а-я][а-я0-9 .’'-]*")
_ADDRESS_FORMAT = str.maketrans("ё–—", "е--")
HOUSE_BLOCK_TAGS = {"блок", "секция", "таунхаус"}
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


def _address_role(component: str) -> str:
    words = component.split()
    terms = {word.strip(".") for word in words}
    ends = {words[0].strip("."), words[-1].strip(".")}
    if words[:2] == ["жилой", "комплекс"]:
        return "territory"
    if ends & _SETTLEMENT_TYPES:
        return "settlement"
    if ends.intersection(_STREET_TYPES):
        return "street"
    if ends & _DISTRICTS:
        return "district"
    if terms & (_TERRITORIES | _DISTRICTS):
        return "territory"
    if ends.intersection((*_TAGS.values(), "дом")):
        return "other"
    return "name"


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
    house = _HOUSE.fullmatch(core[-1])
    if house is not None:
        if len(core) < 3:
            return None
        locality, street, number = core[:-2], core[-2], house[1]
    else:
        street, separator, number = core[-1].rpartition(" ")
        if not separator or not _HOUSE_NUMBER.fullmatch(number):
            return None
        locality = core[:-1]
        street_name, _, house_marker = street.rpartition(" ")
        if street_name and house_marker in ("дом", "д."):
            street = street_name

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
    settlement_words = locality[settlement_index].split()
    kind = None
    if roles[settlement_index] == "settlement":
        kind_index = 0 if settlement_words[0] in _SETTLEMENT_TYPES else -1
        kind = settlement_words.pop(kind_index)
        if not settlement_words or set(settlement_words) & _SETTLEMENT_TYPES:
            return None
    settlement: tuple[str, ...] = (" ".join(settlement_words),)
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
    tail = street.rsplit(" ", 1)
    if len(tail) == 2 and tail[1] in _STREET_TYPES:
        street = f"{tail[1]} {tail[0]}"
    return Address(
        settlement,
        street,
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


def mentions_house_block(value: str) -> bool:
    return any(
        marker is not None and marker[0] in HOUSE_BLOCK_TAGS
        for marker in (_house_marker(_plain(part)) for part in value.split(","))
    )


def _valid_house_identifier(label: str, identifier: str) -> bool:
    return bool(_HOUSE_NUMBER.fullmatch(identifier)) or (
        label not in ("дом", "участок")
        and len(identifier) == 1
        and identifier.isalpha()
    )


def marked_house_ids(value: str | None) -> dict[str, str]:
    """Keep explicit claims even when the full geographic chain is ambiguous."""
    identifiers: dict[str, str] = {}
    ambiguous: set[str] = set()
    for component in (value or "").split(","):
        marker = _house_marker(_plain(component))
        if marker is None:
            continue
        label, identifier = marker
        if not _valid_house_identifier(label, identifier) or (
            label in identifiers and identifiers[label] != identifier
        ):
            ambiguous.add(label)
        identifiers[label] = identifier
    return {
        label: number for label, number in identifiers.items() if label not in ambiguous
    }


def parse_house_address(value: str | None, region: str) -> HouseAddress | None:
    """Read the published chain without turning a plot number into a building."""
    if not value:
        return None
    parts = [_component(_plain(part)) for part in value.split(",")]
    if not all(parts):
        return None
    region_part = (
        parts[0].removesuffix(" обл.") + " область"
        if parts[0].endswith(" обл.")
        else parts[0]
    )
    if region_part == _plain(region) and _address_role(region_part) == "territory":
        parts.pop(0)

    location: list[str] = []
    qualifiers: dict[str, str] = {}
    number = None
    explicit_building = False
    for component in parts:
        marker = _house_marker(component)
        if marker is not None:
            label, identifier = marker
            if not _valid_house_identifier(label, identifier) or label in qualifiers:
                return None
            if label == "дом":
                if number is not None:
                    return None
                number = identifier
                explicit_building = True
            else:
                qualifiers[label] = identifier
            continue
        if _HOUSE_NUMBER.fullmatch(component) and number is None:
            # A bare number belongs to a street only when the street was published.
            if not location or _address_role(location[-1]) != "street" or qualifiers:
                return None
            number = component
            continue
        if number is not None or qualifiers or not component[0].isalpha():
            return None
        street, _, tail = component.rpartition(" ")
        if (
            _HOUSE_NUMBER.fullmatch(tail)
            and street
            and _address_role(street) == "street"
        ):
            component, number = street, tail
        parts_of_street = component.rsplit(" ", 1)
        if len(parts_of_street) == 2 and parts_of_street[1] in _STREET_TYPES:
            component = f"{parts_of_street[1]} {parts_of_street[0]}"
        location.append(component)

    if not location or len(HOUSE_BLOCK_TAGS.intersection(qualifiers)) > 1:
        return None
    roles = [_address_role(part) for part in location]
    territory = any(part.startswith(_HOUSE_TERRITORIES) for part in location)
    has_street = (
        roles[-1] == "street"
        and roles.count("street") == 1
        and (territory or any(role in ("name", "settlement") for role in roles[:-1]))
    )
    rural = any(
        _address_role(part) == "settlement" and "город" not in part.split()
        for part in location
    )
    # A published standalone building or townhouse label may carry the only number.
    if number is None:
        number = qualifiers.get("строение") or qualifiers.get("таунхаус")
        explicit_building = number is not None
    complete = number is not None and (
        has_street or (explicit_building and (territory or rural))
    )
    return HouseAddress(tuple(location), number, qualifiers, complete)
