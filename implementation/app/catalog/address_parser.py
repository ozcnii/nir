"""Parse published address strings into components used for matching."""

import re
import unicodedata

from app.catalog.address import Address

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
_HOUSE = re.compile(rf"(?:дом\s*|д\.\s*)?({_NUMBER})")
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
    parts: list[str], *, room: bool
) -> tuple[int, dict[str, tuple[str, str]]] | None:
    start = len(parts)
    details: dict[str, tuple[str, str]] = {}
    for i, part in enumerate(parts):
        match = _TAG.fullmatch(_plain(part))
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
        details[name] = number, part
        start = min(start, i)
    return start, details


def published_room_details(value: str | None) -> dict[str, tuple[str, str]]:
    """Read explicit qualifiers independently of the geographic address."""
    if not value:
        return {}
    parsed = _address_details([part.strip() for part in value.split(",")], room=True)
    return parsed[1] if parsed is not None else {}


def parse_address(
    value: str | None, region: str, *, room: bool = False
) -> Address | None:
    if not value:
        return None
    parts = [item.strip() for item in value.split(",")]
    if not all(parts):
        return None
    parsed = _address_details(parts, room=room)
    if parsed is None:
        return None
    detail_start, details = parsed
    core = [_component(_plain(item)) for item in parts[:detail_start]]
    first = int(bool(core) and core[0] == _plain(region))
    core = core[first:]
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
    return any(
        _TAGS[match[1]] not in _ROOM_TAGS
        for part in address.split(",")
        if (match := _TAG.fullmatch(_plain(part))) is not None
    )
