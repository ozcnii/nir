"""Filter by locations explicitly identified in published addresses."""

import re

from django.db.models import Q

from app.catalog.models import Ad

LOCATION_NAME_LIMIT = 120

_DISTRICT = r"(?:район|р-н)"
_STATION = r"(?:метро|м\.)"
_COMPONENTS = {
    "district": (rf"{_DISTRICT}\s+{{name}}", rf"{{name}}\s+{_DISTRICT}"),
    "metro": (rf"{_STATION}\s+{{name}}",),
}
LOCATION_FIELDS = tuple(_COMPONENTS)
_EXTRACTORS = {
    field: re.compile(
        "|".join(pattern.format(name=r"(\S(?:[^,]*\S)?)") for pattern in patterns),
        re.IGNORECASE,
    )
    for field, patterns in _COMPONENTS.items()
}


def published_locations(deal: str, region: int | None) -> dict[str, list[str]]:
    ads = Ad.objects.filter(is_active=True, deal_type=deal)
    if region is not None:
        ads = ads.filter(property__region_id=region)
    names: dict[str, dict[str, str]] = {field: {} for field in _EXTRACTORS}
    addresses = (
        ads.order_by("property__address")
        .values_list("property__address", flat=True)
        .distinct()
    )
    for address in addresses.iterator():
        if not address:
            continue
        for part in (chunk.strip() for chunk in address.split(",")):
            for field, pattern in _EXTRACTORS.items():
                if match := pattern.fullmatch(part):
                    label = next(group.strip() for group in match.groups() if group)
                    if len(label) <= LOCATION_NAME_LIMIT:
                        names[field].setdefault(label.casefold(), label)
    return {
        field: [labels[key] for key in sorted(labels)]
        for field, labels in names.items()
    }


def location_conditions(values: dict[str, object]) -> Q:
    result = Q()
    for field, patterns in _COMPONENTS.items():
        if name := values.get(field):
            alternatives = "|".join(
                pattern.format(name=re.escape(str(name))) for pattern in patterns
            )
            result &= Q(
                property__address__iregex=rf"(?:^|,)\s*(?:{alternatives})\s*(?:,|$)"
            )
    return result
