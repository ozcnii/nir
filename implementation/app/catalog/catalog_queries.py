"""Read catalog pages and their matching active offers."""

from dataclasses import dataclass

from django.core.paginator import EmptyPage, Page, Paginator
from django.db.models import Exists, OuterRef, QuerySet, Subquery

from app.catalog.catalog_filters import CatalogFilters
from app.catalog.models import Ad, MarketSegment, PriceHistory, Property
from app.catalog.read_snapshot import read_snapshot
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
)

PAGE_SIZE = 20

_PROPERTY_TYPES = {
    "flat": APARTMENT_TYPE_ID,
    "resale": APARTMENT_TYPE_ID,
    "new_build": APARTMENT_TYPE_ID,
    "room": ROOM_TYPE_ID,
    "house": HOUSE_TYPE_ID,
}


@dataclass(frozen=True)
class CatalogOffer:
    ad: Ad
    latest_price: PriceHistory | None


@dataclass(frozen=True)
class CatalogResult:
    property: Property
    offers: list[CatalogOffer]


def _with_bounds(
    field: str, filters: CatalogFilters, *, quantity: str
) -> dict[str, object]:
    return {
        f"{field}__{operator}": value
        for suffix, operator in (("min", "gte"), ("max", "lte"))
        if (value := filters.cleaned_data.get(f"{quantity}_{suffix}")) is not None
    }


def matching_offers(filters: CatalogFilters) -> QuerySet[Ad]:
    values = filters.cleaned_data
    ads = Ad.objects.filter(is_active=True, deal_type=values["deal_type"])
    category = values["category"]
    if (property_type := _PROPERTY_TYPES.get(category)) is not None:
        ads = ads.filter(property__property_type_id=property_type)
    if category in (MarketSegment.RESALE, MarketSegment.NEW_BUILD):
        ads = ads.filter(market_segment=category)
    if (region := values.get("region_id")) is not None:
        ads = ads.filter(property__region_id=region)
    for quantity, field in (
        ("area", "property__area_m2"),
        ("rooms", "property__rooms"),
        ("floor", "property__floor"),
    ):
        ads = ads.filter(**_with_bounds(field, filters, quantity=quantity))
    price_bounds = _with_bounds("latest_price", filters, quantity="price")
    if price_bounds:
        most_recent = PriceHistory.objects.filter(ad_id=OuterRef("pk")).order_by(
            "-observed_at"
        )
        ads = ads.alias(
            latest_price=Subquery(most_recent.values("price_rub")[:1])
        ).filter(**price_bounds)
    return ads


def _catalog_page(filters: CatalogFilters) -> Page[Property]:
    number: str = filters.cleaned_data["page"]
    # Beyond the range of property IDs there can be no such page.
    if len(number) > 19:
        raise EmptyPage
    ads = matching_offers(filters)
    properties = (
        Property.objects.filter(Exists(ads.filter(property_id=OuterRef("pk"))))
        .select_related("region", "property_type")
        .order_by("pk")
    )
    return Paginator(properties, PAGE_SIZE).page(number)


def _catalog_results(
    page: Page[Property], filters: CatalogFilters
) -> list[CatalogResult]:
    properties = list(page)
    ads = list(
        matching_offers(filters)
        .filter(property__in=properties)
        .order_by("source_id", "external_id")
    )
    latest_prices = {
        price.ad_id: price
        for price in PriceHistory.objects.filter(ad__in=ads)
        .order_by("ad_id", "-observed_at")
        .distinct("ad_id")
    }
    by_property: dict[int, list[CatalogOffer]] = {item.pk: [] for item in properties}
    for ad in ads:
        by_property[ad.property_id].append(CatalogOffer(ad, latest_prices.get(ad.pk)))
    return [CatalogResult(item, by_property[item.pk]) for item in properties]


def read_catalog(filters: CatalogFilters) -> tuple[Page[Property], list[CatalogResult]]:
    """Read the page count, properties, matching offers and prices together."""
    with read_snapshot():
        page = _catalog_page(filters)
        return page, _catalog_results(page, filters)
