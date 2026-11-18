"""Read source settings and manage catalog reference entries."""

from collections.abc import Sequence

from app.catalog.locks import catalog_transaction
from app.catalog.models import PropertyType, Region, Source
from app.catalog.reference_defaults import SOURCE_CODES


def source_enabled(code: str) -> bool | None:
    if code not in SOURCE_CODES:
        return None
    return Source.objects.filter(pk=code).values_list("enabled", flat=True).first()


def delete_references(reference: str, ids: Sequence[int]) -> int:
    if reference not in ("regions", "property_types"):
        raise ValueError("unsupported reference dictionary")
    with catalog_transaction(wait=False):
        records = (
            Region.objects.filter(pk__in=ids)
            if reference == "regions"
            else PropertyType.objects.filter(pk__in=ids)
        )
        deleted, _ = records.delete()
        return deleted
