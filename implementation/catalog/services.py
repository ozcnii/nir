"""Accept and persist normalized publication snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast

from psycopg import Connection
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPrice,
    is_database_text,
    is_http_url,
)
from catalog.matching import (
    FLAT_ID,
    Match,
    PropertyFacts,
    choose_flat,
    repeated_flat_patch,
)
from catalog.matching_address import parse_address
from catalog.models import DealType, MarketSegment, PriceKind
from catalog.queries import flat_candidates, saved_property
from catalog.reference_data_v1 import REGIONS, SOURCE_CODES
from runtime.postgres_locks import CATALOG_WRITE_LOCK

Outcome = Literal[
    "created",
    "updated",
    "unchanged",
    "stale",
    "ignored",
    "conflict",
    "rejected",
    "not_found",
    "unavailable",
]
_REGION_NAMES = dict(REGIONS)


@dataclass(frozen=True)
class Receipt:
    outcome: Outcome
    ad_id: int | None = None
    property_id: int | None = None
    price_changed: bool = False
    reason: str | None = None
    previous_deal_type: Deal | None = None
    incoming_deal_type: Deal | None = None


@dataclass(frozen=True)
class _StoredAd:
    id: int
    property_id: int
    deal_type: Deal
    observed_at: datetime
    is_active: bool
    url: str
    market_segment: str | None
    description: str | None
    photos: list[str]


def _nonblank_database_text(value: object) -> bool:
    return is_database_text(value) and bool(value.strip())


def _decimal_fits_column(
    value: object, minimum: str, maximum: str, digits: int
) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    return Decimal(minimum) <= value <= Decimal(maximum) and value == value.quantize(
        Decimal(1).scaleb(-digits)
    )


def _physical_valid(data: Mapping[str, object]) -> bool:
    if (
        not {
            "address",
            "area_m2",
            "land_area_m2",
            "latitude",
            "longitude",
            "rooms",
            "floor",
            "floors_total",
        }
        <= data.keys()
    ):
        return False
    if data["address"] is not None and not _nonblank_database_text(data["address"]):
        return False
    if (data["latitude"] is None) != (data["longitude"] is None):
        return False
    for key, minimum, maximum, digits in (
        ("latitude", "-90", "90", 7),
        ("longitude", "-180", "180", 7),
        ("area_m2", "0.01", "9999999999.99", 2),
        ("land_area_m2", "0.01", "999999999999.99", 2),
    ):
        value = data[key]
        if value is not None and not _decimal_fits_column(
            value, minimum, maximum, digits
        ):
            return False
    for key, lower_bound in (("rooms", 0), ("floor", -32768), ("floors_total", 1)):
        value = data[key]
        if value is not None and (
            type(value) is not int or not lower_bound <= value <= 32767
        ):
            return False
    floor, total = data["floor"], data["floors_total"]
    return not (isinstance(floor, int) and isinstance(total, int) and floor > total)


def _active_valid(data: Mapping[str, object]) -> bool:
    if not {"description", "market_segment", "price", "photos"} <= data.keys():
        return False
    if not is_http_url(data.get("url")) or data.get("deal_type") not in DealType.values:
        return False
    segment = data["market_segment"]
    if segment is not None and segment not in MarketSegment.values:
        return False
    description = data["description"]
    if description is not None and not is_database_text(description):
        return False
    if not _physical_valid(data):
        return False
    for key in ("region_id", "property_type_id"):
        identifier = data.get(key)
        if type(identifier) is not int or not 0 < identifier < 2**63:
            return False
    price, photos = data["price"], data["photos"]
    if not isinstance(price, dict) or not isinstance(photos, dict):
        return False
    price_ok = price.get("status") == "unavailable" or (
        price.get("status") == "known"
        and price.get("currency") == "RUB"
        and price.get("price_kind") in PriceKind.values
        and _decimal_fits_column(price.get("price_rub"), "0", "99999999999999.99", 2)
    )
    urls = photos.get("urls")
    photos_ok = photos.get("status") == "unavailable" or (
        photos.get("status") == "known"
        and isinstance(urls, list)
        and all(is_http_url(url) for url in urls)
        and len(set(urls)) == len(urls)
    )
    return price_ok and photos_ok


def accept_snapshot(
    connection: Connection[TupleRow], snapshot: Mapping[str, object]
) -> Receipt:
    """Accept one P3 snapshot on the caller's already-fenced connection."""
    source = snapshot.get("source_code")
    external_id = snapshot.get("external_id")
    observed_at = snapshot.get("observed_at")
    state = snapshot.get("state")
    if (
        source not in SOURCE_CODES
        or not _nonblank_database_text(external_id)
        or not isinstance(observed_at, datetime)
        or observed_at.utcoffset() is None
        or state not in ("active", "removed", "unavailable")
    ):
        return Receipt("rejected", reason="invalid_envelope")
    if state == "unavailable":
        return Receipt("unavailable")

    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", CATALOG_WRITE_LOCK)
        with connection.cursor(row_factory=class_row(_StoredAd)) as cursor:
            previous = cursor.execute(
                """
                SELECT id, property_id, deal_type, observed_at, is_active,
                       url, market_segment, description, photos
                FROM ads WHERE source_code=%s AND external_id=%s
                """,
                (source, external_id),
            ).fetchone()
        if previous is not None and observed_at <= previous.observed_at:
            return Receipt(
                "ignored" if observed_at == previous.observed_at else "stale",
                previous.id,
                previous.property_id,
            )
        deal = snapshot.get("deal_type")
        if (
            previous is not None
            and deal in DealType.values
            and deal != previous.deal_type
        ):
            return Receipt(
                "conflict",
                previous.id,
                previous.property_id,
                reason="deal_type_changed",
                previous_deal_type=previous.deal_type,
                incoming_deal_type=cast(Deal, deal),
            )
        if state == "removed":
            if deal not in DealType.values or not is_http_url(snapshot.get("url")):
                return Receipt("rejected", reason="invalid_removed_snapshot")
            if previous is None:
                return Receipt("not_found")
            connection.execute(
                "UPDATE ads SET is_active=false, observed_at=%s WHERE id=%s",
                (observed_at, previous.id),
            )
            return Receipt(
                "updated" if previous.is_active else "unchanged",
                previous.id,
                previous.property_id,
            )
        if not _active_valid(snapshot):
            return Receipt("rejected", reason="invalid_active_snapshot")
        active_data = cast(ActiveSnapshot, snapshot)
        photos = active_data["photos"]
        checks = connection.execute(
            """
            SELECT public.catalog_photos_valid(%(photo_urls)s),
                   EXISTS(SELECT FROM sources WHERE code=%(source_code)s)
               AND EXISTS(SELECT FROM regions WHERE id=%(region_id)s)
               AND EXISTS(SELECT FROM property_types WHERE id=%(property_type_id)s)
            """,
            {
                **active_data,
                "photo_urls": Jsonb(
                    photos["urls"] if photos["status"] == "known" else []
                ),
            },
        ).fetchone()
        assert checks is not None
        photos_valid, references_exist = checks
        if not references_exist:
            return Receipt("rejected", reason="unknown_reference")
        match: Match | None = None
        region = _REGION_NAMES.get(active_data["region_id"])
        if previous is not None:
            property_id = previous.property_id
            saved = saved_property(connection, property_id)
            if (
                saved["property_type_id"] == FLAT_ID
                or active_data["property_type_id"] == FLAT_ID
            ):
                if (
                    saved["region_id"] != active_data["region_id"]
                    or saved["property_type_id"] != active_data["property_type_id"]
                ):
                    return Receipt(
                        "conflict",
                        previous.id,
                        property_id,
                        reason="property_scope_changed",
                    )
                if region is not None:
                    patch = repeated_flat_patch(saved, active_data, region)
                    if patch is None:
                        return Receipt(
                            "conflict",
                            previous.id,
                            property_id,
                            reason="apartment_address_changed",
                        )
                    match = Match(property_id, patch)
        elif active_data["property_type_id"] == FLAT_ID and region is not None:
            address = parse_address(active_data["address"], region)
            if address is not None:
                match = choose_flat(
                    active_data,
                    address,
                    flat_candidates(connection, active_data["region_id"]),
                    region,
                )
        if not photos_valid:
            return Receipt("rejected", reason="invalid_active_snapshot")
        return _store_active(connection, active_data, previous, match)


def _store_active(
    connection: Connection[TupleRow],
    data: ActiveSnapshot,
    previous: _StoredAd | None,
    match: Match | None,
) -> Receipt:
    photo_state = data["photos"]
    known_photos = photo_state["status"] == "known"
    photos = photo_state["urls"] if photo_state["status"] == "known" else []
    photo_time = data["observed_at"] if known_photos else None
    if previous is None:
        if match is not None:
            property_id = match.property_id
            _apply_property_patch(connection, property_id, match.patch)
        else:
            property_row = connection.execute(
                """
                INSERT INTO properties (region_id, property_type_id, address, latitude,
                                        longitude, area_m2, rooms, floor, floors_total, land_area_m2)
                VALUES (%(region_id)s, %(property_type_id)s, %(address)s, %(latitude)s,
                        %(longitude)s, %(area_m2)s, %(rooms)s, %(floor)s,
                        %(floors_total)s, %(land_area_m2)s) RETURNING id
                """,
                data,
            ).fetchone()
            assert property_row is not None
            property_id = cast(int, property_row[0])
        ad = connection.execute(
            """
            INSERT INTO ads (property_id, source_code, external_id, url, deal_type, market_segment,
                             description, is_active, observed_at, photos, photos_observed_at)
            VALUES (%(property_id)s, %(source_code)s, %(external_id)s, %(url)s, %(deal_type)s,
                    %(market_segment)s, %(description)s, true, %(observed_at)s,
                    %(image_urls)s, %(photo_time)s) RETURNING id
            """,
            {
                **data,
                "property_id": property_id,
                "image_urls": Jsonb(photos),
                "photo_time": photo_time,
            },
        ).fetchone()
        assert ad is not None
        ad_id = cast(int, ad[0])
        changed = False
    else:
        ad_id, property_id = previous.id, previous.property_id
        changed = (
            True,
            data["url"],
            data["market_segment"],
            data["description"],
        ) != (
            previous.is_active,
            previous.url,
            previous.market_segment,
            previous.description,
        )
        changed |= known_photos and photos != previous.photos
        if match is not None:
            changed |= _apply_property_patch(connection, match.property_id, match.patch)
        connection.execute(
            """
            UPDATE ads SET url=%(url)s, market_segment=%(market_segment)s,
                           description=%(description)s, is_active=true, observed_at=%(observed_at)s,
                           photos=COALESCE(%(image_urls)s, photos),
                           photos_observed_at=COALESCE(%(photo_time)s, photos_observed_at)
            WHERE id=%(ad_id)s
            """,
            {
                **data,
                "ad_id": ad_id,
                "image_urls": Jsonb(photos) if known_photos else None,
                "photo_time": photo_time,
            },
        )
    price = data["price"]
    price_changed = price["status"] == "known" and _record_price(
        connection, ad_id, data["observed_at"], price
    )
    outcome: Outcome = (
        "created"
        if previous is None
        else "updated"
        if changed or price_changed
        else "unchanged"
    )
    return Receipt(outcome, ad_id, property_id, price_changed)


def _apply_property_patch(
    connection: Connection[TupleRow], property_id: int, patch: PropertyFacts
) -> bool:
    if all(value is None for value in patch.values()):
        return False
    connection.execute(
        """
        UPDATE properties SET
            address=COALESCE(%(address)s, address),
            latitude=COALESCE(%(latitude)s, latitude),
            longitude=COALESCE(%(longitude)s, longitude),
            area_m2=COALESCE(%(area_m2)s, area_m2),
            rooms=COALESCE(%(rooms)s, rooms),
            floor=COALESCE(%(floor)s, floor),
            floors_total=COALESCE(%(floors_total)s, floors_total),
            land_area_m2=COALESCE(%(land_area_m2)s, land_area_m2)
        WHERE id=%(id)s
        """,
        {**patch, "id": property_id},
    )
    return True


def _record_price(
    connection: Connection[TupleRow],
    ad_id: int,
    observed_at: datetime,
    price: KnownPrice,
) -> bool:
    pair = (price["price_rub"], price["price_kind"])
    previous = connection.execute(
        """
        SELECT price_rub, price_kind FROM price_history
        WHERE ad_id=%s ORDER BY observed_at DESC LIMIT 1
        """,
        (ad_id,),
    ).fetchone()
    if previous == pair:
        return False
    connection.execute(
        """
        INSERT INTO price_history (ad_id, observed_at, price_rub, price_kind)
        VALUES (%s, %s, %s, %s)
        """,
        (ad_id, observed_at, *pair),
    )
    return True
