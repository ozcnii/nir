"""Read collection parameters as values owned by the catalog."""

from catalog.models import PropertyType, Region, Source
from catalog.reference_data_v1 import PROPERTY_TYPES, REGIONS, SOURCE_CODES

_PROPERTY_KINDS = {pk: kind for pk, kind, _ in PROPERTY_TYPES}
_REGION_NAMES = dict(REGIONS)


def source_enabled(code: str) -> bool | None:
    """Return the current setting, or None for an unsupported or absent source."""
    if code not in SOURCE_CODES:
        return None
    return Source.objects.filter(pk=code).values_list("enabled", flat=True).first()


def property_kind(property_type_id: int) -> str | None:
    """Use the standard ID to resolve the physical kind, independent of its label."""
    kind = _PROPERTY_KINDS.get(property_type_id)
    if kind is not None and PropertyType.objects.filter(pk=property_type_id).exists():
        return kind
    return None


def region_name(region_id: int) -> str | None:
    """Use the standard ID to resolve the canonical region, independent of its label."""
    name = _REGION_NAMES.get(region_id)
    if name is not None and Region.objects.filter(pk=region_id).exists():
        return name
    return None
