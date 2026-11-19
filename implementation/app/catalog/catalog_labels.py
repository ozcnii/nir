"""Russian captions for public deals, segments and property types."""

from app.catalog.models import DealType, MarketSegment, Property
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, ROOM_TYPE_ID

DEAL_LABELS = {
    DealType.SALE: "Продажа",
    DealType.MONTH_RENT: "Месячная аренда",
    DealType.DAY_RENT: "Суточная аренда",
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
