"""Read catalog pages and their matching active offers."""

from dataclasses import dataclass

from django.conf import settings
from django.core.paginator import EmptyPage, Page, Paginator
from django.db.models import Exists, F, OuterRef, QuerySet, Subquery

from app.catalog.catalog_filters import CatalogFilters
from app.catalog.location_filters import location_conditions
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
    ads = ads.filter(location_conditions(values))
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


def lowest_quote(ads: QuerySet[Ad]) -> tuple[Subquery, Subquery]:
    latest = PriceHistory.objects.filter(ad_id=OuterRef("pk")).order_by("-observed_at")
    cheapest = (
        ads.filter(property_id=OuterRef("pk"))
        .annotate(
            quote=Subquery(latest.values("price_rub")[:1]),
            quote_kind=Subquery(latest.values("price_kind")[:1]),
        )
        .order_by(F("quote").asc(nulls_last=True), "source_id", "external_id")
    )
    return (
        Subquery(cheapest.values("quote")[:1]),
        Subquery(cheapest.values("quote_kind")[:1]),
    )


def sort_properties(properties: QuerySet[Property], sort: str) -> QuerySet[Property]:
    if not sort:
        if demo_prefix := getattr(settings, "CATALOG_DEMO_PREFIX", ""):
            demo = Ad.objects.filter(
                property_id=OuterRef("pk"), external_id__startswith=demo_prefix
            )
            return properties.order_by(Exists(demo).desc(), "pk")
        return properties.order_by("pk")
    field, _, direction = sort.partition("_")
    value = F("quote" if field == "price" else "area_m2")
    ordering = (
        value.asc(nulls_last=True)
        if direction == "asc"
        else value.desc(nulls_last=True)
    )
    return properties.order_by(ordering, "pk")


def _catalog_page(filters: CatalogFilters) -> Page[Property]:
    number: str = filters.cleaned_data["page"]
    # Beyond the range of property IDs there can be no such page.
    if len(number) > 19:
        raise EmptyPage
    ads = matching_offers(filters)
    properties = Property.objects.filter(
        Exists(ads.filter(property_id=OuterRef("pk")))
    ).select_related("region", "property_type")
    sort = filters.cleaned_data["sort"]
    if sort.startswith("price_"):
        quote, _ = lowest_quote(ads)
        properties = properties.alias(quote=quote)
    return Paginator(sort_properties(properties, sort), PAGE_SIZE).page(number)


def _with_prices(properties: list[Property], ads: QuerySet[Ad]) -> list[CatalogResult]:
    offers = list(
        ads.filter(property__in=properties).order_by("source_id", "external_id")
    )
    latest_prices = {
        price.ad_id: price
        for price in PriceHistory.objects.filter(ad__in=offers)
        .order_by("ad_id", "-observed_at")
        .distinct("ad_id")
    }
    by_property: dict[int, list[CatalogOffer]] = {item.pk: [] for item in properties}
    for ad in offers:
        by_property[ad.property_id].append(CatalogOffer(ad, latest_prices.get(ad.pk)))
    return [CatalogResult(item, by_property[item.pk]) for item in properties]


def read_catalog(filters: CatalogFilters) -> tuple[Page[Property], list[CatalogResult]]:
    """Read the page count, properties, matching offers and prices together."""
    with read_snapshot():
        page = _catalog_page(filters)
        return page, _with_prices(list(page), matching_offers(filters))


def read_selected(deal: str, property_ids: list[int]) -> dict[int, CatalogResult]:
    """Read saved objects with the active offers for their selected deal."""
    with read_snapshot():
        properties = list(
            Property.objects.filter(pk__in=property_ids).select_related(
                "region", "property_type"
            )
        )
        ads = Ad.objects.filter(is_active=True, deal_type=deal)
        return {item.property.pk: item for item in _with_prices(properties, ads)}
