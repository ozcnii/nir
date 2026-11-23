"""Russian captions for public deals, segments and property types."""

import re

from app.catalog.models import DealType, MarketSegment, Property
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, ROOM_TYPE_ID

DEAL_LABELS = {
    DealType.SALE: "Продажа",
    DealType.MONTH_RENT: "Месячная аренда",
    DealType.DAY_RENT: "Суточная аренда",
}
DEAL_TABS = {
    DealType.SALE: "Продажа",
    DealType.MONTH_RENT: "Аренда на месяц",
    DealType.DAY_RENT: "Аренда посуточно",
}
MARKET_LABELS: dict[str, str] = {
    MarketSegment.RESALE: "Вторичка",
    MarketSegment.NEW_BUILD: "Новостройка",
}
PRICE_UNITS: dict[str, str] = {
    DealType.SALE: "руб.",
    DealType.MONTH_RENT: "руб./мес.",
    DealType.DAY_RENT: "руб./сут.",
}


def property_title(item: Property) -> str:
    rooms = item.rooms
    if item.property_type_id == APARTMENT_TYPE_ID and rooms is not None:
        return f"{rooms}-комнатная квартира" if rooms else "Студия"
    if item.property_type_id == ROOM_TYPE_ID and rooms is not None and rooms > 1:
        ending = rooms % 10 if not 11 <= rooms % 100 <= 14 else 0
        noun = "комната" if ending == 1 else "комнаты" if 2 <= ending <= 4 else "комнат"
        return f"{rooms} {noun}"
    return item.property_type.name


def property_location(item: Property) -> str:
    region = item.region.name
    if not item.address:
        return f"Адрес не указан · {region}"
    if region.casefold() in item.address.casefold():
        return item.address
    return f"{item.address} · {region}"


def compact_property_location(item: Property, *, show_region: bool = True) -> str:
    """Keep the published address components useful in a small result card."""
    if not item.address:
        return property_location(item) if show_region else "Адрес не указан"

    region = item.region.name
    parts = [part.strip() for part in item.address.split(",") if part.strip()]
    parts = [part for part in parts if part.casefold() != region.casefold()]
    if not parts:
        return region
    visible: list[str] = []
    for index, part in enumerate(parts):
        suffix = " городское поселение"
        if part.casefold().endswith(suffix) and index + 1 < len(parts):
            locality = re.sub(
                r"(?:^пгт\.?\s+|\s+пгт\.?$)", "", parts[index + 1], flags=re.I
            )
            if part[: -len(suffix)].casefold() == locality.casefold():
                continue
        part = re.sub(r"^(?:район|р-н)\s+(.+)$", r"\1 район", part, flags=re.I)
        part = re.sub(r"^(.+)\s+(ЖК|пгт)$", r"\2 \1", part, flags=re.I)
        part = re.sub(r"^улица\s+", "ул. ", part, flags=re.I)
        if part.casefold() not in {entry.casefold() for entry in visible}:
            visible.append(part)
    address = ", ".join(visible)
    if show_region and re.match(
        r"^(?:ул\.|переулок|пер\.|проспект|бульвар|шоссе)\s", visible[0], flags=re.I
    ):
        return f"{address} · {region}"
    return address
