"""Read one property's complete publication and price history snapshot."""

from dataclasses import dataclass

from django.db import connection, transaction

from app.catalog.catalog_filters import BIGINT_MAX, CatalogFilters
from app.catalog.catalog_queries import matching_offers
from app.catalog.models import Ad, PriceHistory, Property


@dataclass(frozen=True)
class PropertyOffer:
    ad: Ad
    price_history: list[PriceHistory]
    matches_search: bool

    @property
    def latest_price(self) -> PriceHistory | None:
        return self.price_history[-1] if self.price_history else None


@dataclass(frozen=True)
class PropertyDetails:
    property: Property
    offers: list[PropertyOffer]


def property_details(
    property_id: int, filters: CatalogFilters | None = None
) -> PropertyDetails | None:
    """Eagerly read the object and offers before releasing the database snapshot."""
    if not 1 <= property_id <= BIGINT_MAX:
        return None
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        item = (
            Property.objects.select_related("region", "property_type")
            .filter(pk=property_id)
            .first()
        )
        if item is None:
            return None

        ads = list(
            Ad.objects.filter(property_id=property_id).order_by(
                "source_id", "external_id"
            )
        )
        histories: dict[int, list[PriceHistory]] = {ad.pk: [] for ad in ads}
        for price in PriceHistory.objects.filter(ad__in=ads).order_by("observed_at"):
            histories[price.ad_id].append(price)
        matching_ids = (
            set(
                matching_offers(filters)
                .filter(property_id=property_id)
                .values_list("pk", flat=True)
            )
            if filters is not None
            else set()
        )
        return PropertyDetails(
            item,
            [PropertyOffer(ad, histories[ad.pk], ad.pk in matching_ids) for ad in ads],
        )
