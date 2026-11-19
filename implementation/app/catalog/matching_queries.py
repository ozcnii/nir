"""Read property facts and candidates for apartment matching."""

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


def flat_candidates(region_id: int) -> list[Candidate]:
    rows = (
        Property.objects.filter(
            region_id=region_id,
            property_type_id=APARTMENT_TYPE_ID,
            address__isnull=False,
        )
        .values("id", *PROPERTY_FACT_FIELDS)
        .annotate(
            descriptions=ArrayAgg(
                "ad__description", filter=Q(ad__description__isnull=False), default=[]
            )
        )
    )
    return [
        Candidate(row["id"], cast(PropertyFacts, row), row["descriptions"])
        for row in rows
    ]
