"""Interpret and preserve the address evidence used by matching."""

import re
import unicodedata
from dataclasses import dataclass

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
_HOUSE = re.compile(rf"(?:дом\s*|д\.\s*)?({_NUMBER})\Z")
_STREET_AND_NUMBER = re.compile(
    rf"(?P<street>.+?) +(?:(?:дом|д\.) +)?(?P<num>{_NUMBER})\Z"
)
_ADDRESS_TOKEN = re.compile(r"[а-я][а-я0-9 .’'-]*\Z")
_UNIT_NUMBER = re.compile(rf"{_NUMBER}\Z")


def _plain(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFC", value).lower().replace("ё", "е").split()
    )


def _component(value: str) -> str:
    head, space, tail = value.partition(" ")
    return _PREFIXES.get(head, head) + space + tail


def _address_role(component: str) -> str:
    words = component.replace("–", "-").split()
    terms = {word.strip(".") for word in words}
    ends = {words[0].strip("."), words[-1].strip(".")}
    if ends & _SETTLEMENT_TYPES:
        return "settlement"
    if ends.intersection(_STREET_TYPES):
        return "street"
    if ends & _DISTRICTS:
        return "district"
    if terms & (_TERRITORIES | _DISTRICTS) or component.startswith("жилой комплекс "):
        return "territory"
    if ends.intersection((*_TAGS.values(), "дом")):
        return "other"
    return "name"


@dataclass(frozen=True)
class Address:
    locality: tuple[str, ...]
    street: str
    district: str | None
    number: str
    details: dict[str, tuple[str, str]]
    settlement_kind: str | None
    settlement_index: int
    settlement_text: str
    district_text: str | None

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
        parts[first + settlement_index],
        district_text,
    )


def apartment_address_conflicts(previous: Address, incoming: Address) -> bool:
    district_conflict = (
        previous.district is not None
        and incoming.district is not None
        and previous.district != incoming.district
    )
    return (
        district_conflict
        or (
            previous.settlement_kind is not None
            and incoming.settlement_kind is not None
            and previous.settlement_kind != incoming.settlement_kind
        )
        or previous.base() != incoming.base()
        or any(
            previous.details[name][0] != incoming.details[name][0]
            for name in previous.details.keys() & incoming.details.keys()
        )
    )


def address_patch(
    previous: Address, incoming: Address, address: str | None
) -> str | None:
    if address is None:
        return None
    parts = [part.strip() for part in address.split(",")]
    updated = parts.copy()
    index = previous.settlement_index
    if previous.settlement_kind is None and incoming.settlement_kind is not None:
        updated[index] = incoming.settlement_text
    if previous.district is None and incoming.district_text is not None:
        updated.insert(index + 1, incoming.district_text)
    updated.extend(
        value[1]
        for key, value in incoming.details.items()
        if key not in previous.details
    )
    return ", ".join(updated) if updated != parts else None


def mentions_unit(address: str) -> bool:
    return any(_TAG.fullmatch(_plain(part)) for part in address.split(","))
