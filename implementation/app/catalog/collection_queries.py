"""Catalog reads needed by a foreground collection run."""

from app.catalog.contracts import Deal
from app.catalog.models import Ad, PropertyType, Region


def scope_exists(region_id: int, property_type_id: int) -> bool:
    return (
        Region.objects.filter(pk=region_id).exists()
        and PropertyType.objects.filter(pk=property_type_id).exists()
    )


def active_publications(
    source: str, region_id: int, property_type_id: int, deal: Deal
) -> dict[str, str]:
    return dict(
        Ad.objects.filter(
            source_id=source,
            property__region_id=region_id,
            property__property_type_id=property_type_id,
            deal_type=deal,
            is_active=True,
        )
        .order_by("id")
        .values_list("external_id", "url")
        .iterator()
    )
