"""Read collection parameters as values owned by the catalog."""

from typing import cast

from psycopg import Connection
from psycopg.rows import TupleRow, dict_row

from catalog.matching import FLAT_ID, Candidate, PropertyFacts
from catalog.models import DealType, PropertyType, Region, Source
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


def valid_deal_type(value: str) -> bool:
    return value in DealType.values


class SavedProperty(PropertyFacts):
    region_id: int
    property_type_id: int


def property_for_update(
    connection: Connection[TupleRow], property_id: int
) -> SavedProperty:
    with connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            """
            SELECT region_id, property_type_id, address, latitude, longitude,
                   area_m2, rooms, floor, floors_total, land_area_m2
            FROM properties WHERE id=%s
            """,
            (property_id,),
        ).fetchone()
    assert row is not None
    return cast(SavedProperty, row)


def matching_candidates(
    connection: Connection[TupleRow], region_id: int, property_type_id: int
) -> list[Candidate]:
    with connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            """
            SELECT properties.id, address, latitude, longitude, area_m2, rooms,
                   floor, floors_total, land_area_m2,
                   COALESCE(
                     array_agg(ads.description) FILTER (WHERE ads.description IS NOT NULL),
                     ARRAY[]::text[]
                   ) AS descriptions
            FROM properties
            LEFT JOIN ads ON ads.property_id=properties.id AND properties.property_type_id=%s
            WHERE region_id=%s AND property_type_id=%s AND address IS NOT NULL
            GROUP BY properties.id
            """,
            (FLAT_ID, region_id, property_type_id),
        ).fetchall()
    return [
        Candidate(
            cast(int, row["id"]),
            cast(PropertyFacts, row),
            cast(list[str], row["descriptions"]),
        )
        for row in rows
    ]
