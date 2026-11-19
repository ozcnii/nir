"""Parse published address strings into components used for matching."""

import re
import unicodedata

from app.catalog.address import Address, AddressNumbers

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
_HOUSE_MARKERS = {"дом", "д."}
_HOUSE = re.compile(rf"(дом\s*|д\.\s*)?({_NUMBER})")
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


def _building_component(component: str) -> tuple[str | None, str, bool] | None:
    house = _HOUSE.fullmatch(component)
    if house is not None:
        return None, house[2], house[1] is not None
    street, _, number = component.rpartition(" ")
    if not street or not _HOUSE_NUMBER.fullmatch(number):
        return None
    street_name, _, marker = street.rpartition(" ")
    if marker in _HOUSE_MARKERS:
        return street_name, number, True
    return street, number, False


def parse_address(
    value: str | None, region: str, *, room: bool = False
) -> Address | None:
    parsed = _read_address(value, region, room=room)
    return parsed if isinstance(parsed, Address) else None


def read_room_address(
    value: str | None, region: str | None
) -> Address | AddressNumbers | None:
    """Read explicit numbers even if the geographic address is incomplete."""
    return _read_address(value, region, room=True)


def _read_address(
    value: str | None, region: str | None, *, room: bool
) -> Address | AddressNumbers | None:
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
    first = int(region is not None and bool(core) and core[0] == _plain(region))
    core = core[first:]
    building = _building_component(core[-1]) if core else None
    if building is None:
        return AddressNumbers(None, details)
    inline_street, number, explicit = building
    if explicit and any(
        original[2]
        for part in core[:-1]
        if (original := _building_component(part)) is not None
    ):
        return None
    partial = AddressNumbers(number if explicit else None, details)
    if region is None or len(core) < 2:
        return partial
    if inline_street is None:
        locality, street = core[:-2], core[-2]
    else:
        locality, street = core[:-1], inline_street
    if not all(_ADDRESS_TOKEN.fullmatch(part) for part in (*locality, street)):
        return partial
    roles = [_address_role(part) for part in locality]
    if _address_role(street) not in ("street", "name"):
        return partial
    # Only one component may identify the settlement. Districts can qualify it.
    settlements = [i for i, role in enumerate(roles) if role in ("settlement", "name")]
    districts = [i for i, role in enumerate(roles) if role == "district"]
    if (
        len(settlements) != 1
        or len(districts) > 1
        or any(role not in ("settlement", "name", "district") for role in roles)
    ):
        return partial
    settlement_index = settlements[0]
    settlement_words = locality[settlement_index].split()
    kind = None
    if roles[settlement_index] == "settlement":
        kind_index = 0 if settlement_words[0] in _SETTLEMENT_TYPES else -1
        kind = settlement_words.pop(kind_index)
        if not settlement_words or set(settlement_words) & _SETTLEMENT_TYPES:
            return partial
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
            return partial
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
