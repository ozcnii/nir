"""Read and compare published offers on a common basis."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.urls import reverse

from app.catalog.catalog_labels import (
    MARKET_LABELS,
    compact_property_location,
    property_location,
    property_title,
)
from app.catalog.catalog_queries import CatalogResult
from app.catalog.models import DealType, PriceHistory, PriceKind, Property
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    SOURCE_LABELS,
)

MAX_COMPARED = 6
_PRIORITY_REASONS = {
    "price": "Берём минимальную точную цену объекта. Цены «от» исключены.",
    "area": "Сравниваем указанную площадь объектов одной категории.",
    "per_meter": "Делим точную опубликованную цену квартиры на её площадь.",
}


@dataclass(frozen=True)
class ComparedOffer:
    source: str
    url: str
    price: PriceHistory | None
    per_meter: Decimal | None
    segment: str | None = None
    source_code: str = ""


@dataclass(frozen=True)
class ComparedProperty:
    id: int
    title: str
    address: str = ""
    short_address: str = ""
    asset: Property | None = None
    offers: tuple[ComparedOffer, ...] = ()
    photos: Sequence[str] = ()
    photo_source: str = ""
    url: str = ""

    @property
    def lead_price(self) -> PriceHistory | None:
        return min(
            (offer.price for offer in self.offers if offer.price is not None),
            key=lambda price: price.price_rub,
            default=None,
        )

    @property
    def lead_price_per_meter(self) -> Decimal | None:
        return min(
            (offer.per_meter for offer in self.offers if offer.per_meter is not None),
            default=None,
        )


def selected_property(
    property_id: int, result: CatalogResult | None, deal: str
) -> ComparedProperty:
    if result is None:
        return ComparedProperty(property_id, "Объект недоступен")

    item = result.property
    area = item.area_m2
    offers = []
    for offer in result.offers:
        price = offer.latest_price
        per_meter = (
            price.price_rub / area
            if deal == DealType.SALE
            and item.property_type_id == APARTMENT_TYPE_ID
            and area is not None
            and area > 0
            and price is not None
            and price.price_kind == PriceKind.LISTED
            else None
        )
        offers.append(
            ComparedOffer(
                SOURCE_LABELS.get(offer.ad.source_id, offer.ad.source_id),
                offer.ad.url,
                price,
                per_meter,
                MARKET_LABELS.get(offer.ad.market_segment or ""),
                offer.ad.source_id,
            )
        )

    photo_ad = next((offer.ad for offer in result.offers if offer.ad.photos), None)
    return ComparedProperty(
        id=property_id,
        title=property_title(item),
        address=property_location(item),
        short_address=compact_property_location(item),
        asset=item,
        offers=tuple(offers),
        photos=photo_ad.photos if photo_ad else (),
        photo_source=SOURCE_LABELS.get(photo_ad.source_id, photo_ad.source_id)
        if photo_ad
        else "",
        url=reverse("property_detail", args=[property_id]),
    )


def _metric(item: ComparedProperty, priority: str) -> Decimal | None:
    if not item.offers or item.asset is None:
        return None
    if priority == "area":
        return item.asset.area_m2
    if priority == "per_meter":
        return item.lead_price_per_meter
    return min(
        (
            offer.price.price_rub
            for offer in item.offers
            if offer.price is not None and offer.price.price_kind == PriceKind.LISTED
        ),
        default=None,
    )


def recommendation(
    items: list[ComparedProperty], priority: str, *, budget: bool = False
) -> dict[str, object]:
    categories = {
        item.asset.property_type_id
        for item in items
        if item.asset is not None and item.offers
    }
    if len(categories) > 1:
        return {"message": "Для подсказки сравните объекты одной категории."}

    known = [
        (item, value)
        for item in items
        if (value := _metric(item, priority)) is not None
    ]
    if not known:
        message = (
            "Нет подходящих вариантов с известным значением в этом бюджете."
            if budget
            else "Для подсказки нужны значения хотя бы по двум объектам."
        )
        return {"message": message}
    if len(known) == 1 and (not budget or len(items) != 1):
        return {
            "message": "Значение критерия есть у одного объекта. Для сравнения нужны два."
        }
    known.sort(key=lambda point: point[1], reverse=priority == "area")
    best_value = known[0][1]
    best = [item for item, value in known if value == best_value]
    gap = abs(best_value - known[1][1]) if len(known) >= 2 and len(best) == 1 else None
    return {
        "best": best[0] if len(best) == 1 else None,
        "value": best_value,
        "included": len(known),
        "ties": len(best),
        "tied": best if len(best) > 1 else [],
        "reason": _PRIORITY_REASONS[priority],
        "only_in_budget": budget and len(items) == 1,
        "gap": gap,
    }
