"""Read property facts and candidates for matching."""

from collections.abc import Iterator
from typing import cast

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Q

from app.catalog.contracts import PROPERTY_FACT_FIELDS, PropertyFacts
from app.catalog.matching import Candidate
from app.catalog.models import Property
from app.catalog.reference_defaults import APARTMENT_TYPE_ID


class SavedProperty(PropertyFacts):
    region_id: int
    property_type_id: int


def saved_property(property_id: int) -> SavedProperty:
    row = Property.objects.values(
        "region_id", "property_type_id", *PROPERTY_FACT_FIELDS
    ).get(pk=property_id)
    return cast(SavedProperty, row)


def matching_candidates(region_id: int, property_type_id: int) -> Iterator[Candidate]:
    rows = Property.objects.filter(
        region_id=region_id,
        property_type_id=property_type_id,
        address__isnull=False,
    ).values("id", *PROPERTY_FACT_FIELDS)
    if property_type_id == APARTMENT_TYPE_ID:
        rows = rows.annotate(
            descriptions=ArrayAgg(
                "ad__description", filter=Q(ad__description__isnull=False), default=[]
            )
        )
    for row in rows:
        yield Candidate(
            row["id"], cast(PropertyFacts, row), row.get("descriptions", [])
        )
