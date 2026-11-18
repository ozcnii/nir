"""Transactional catalog mutations and reference deletion fencing."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from django.db import transaction
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from catalog.contracts import ActiveSnapshot, Deal, KnownPrice, is_http_url
from catalog.models import DealType, MarketSegment, PriceKind, PropertyType, Region
from catalog.reference_data_v1 import SOURCE_CODES
from config.collection_lock import try_delete_lock
from config.control import CONTROL_PATH, ControlError, request


class ReferenceDeleteBlocked(RuntimeError):
    pass


def delete_references(
    reference: str,
    ids: Sequence[int],
    *,
    socket_path: Path = CONTROL_PATH,
) -> int:
    if reference not in ("regions", "property_types"):
        raise ValueError("unsupported reference dictionary")
    with transaction.atomic():
        if not try_delete_lock():
            raise ReferenceDeleteBlocked("collection or reference deletion is active")
        try:
            status = request({"action": "status"}, socket_path)
        except ControlError as error:
            raise ReferenceDeleteBlocked("collection status is unavailable") from error
        if (
            status.get("ok") is not True
            or status.get("state") not in ("idle", "finished")
            or status.get("cleanup_confirmed") is not True
        ):
            raise ReferenceDeleteBlocked("collection cleanup has not been confirmed")
        records = (
            Region.objects.filter(pk__in=ids)
            if reference == "regions"
            else PropertyType.objects.filter(pk__in=ids)
        )
        deleted, _ = records.delete()
        return deleted


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
_CATALOG_WRITE_KEY = (0x4E4952, 2)


@dataclass(frozen=True)
class Receipt:
    outcome: Outcome
    ad_id: int | None = None
    property_id: int | None = None
    price_changed: bool = False
    reason: str | None = None
    previous_deal_type: Deal | None = None
    incoming_deal_type: Deal | None = None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _decimal(value: object, minimum: str, maximum: str, digits: int) -> bool:
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
    if data["address"] is not None and not _text(data["address"]):
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
        if value is not None and not _decimal(value, minimum, maximum, digits):
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
    if (
        not {"description", "market_segment", "price", "photos"} <= data.keys()
        or not is_http_url(data.get("url"))
        or data.get("deal_type") not in DealType.values
        or (
            data["market_segment"] is not None
            and data["market_segment"] not in MarketSegment.values
        )
        or (
            data["description"] is not None and not isinstance(data["description"], str)
        )
        or not _physical_valid(data)
    ):
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
        and _decimal(price.get("price_rub"), "0", "99999999999999.99", 2)
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
        or not _text(external_id)
        or not isinstance(observed_at, datetime)
        or observed_at.utcoffset() is None
        or state not in ("active", "removed", "unavailable")
    ):
        return Receipt("rejected", reason="invalid_envelope")
    if state == "unavailable":
        return Receipt("unavailable")

    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", _CATALOG_WRITE_KEY)
        previous = connection.execute(
            """
            SELECT id, property_id, deal_type, observed_at, is_active,
                   url, market_segment, description, photos
            FROM ads WHERE source_code=%s AND external_id=%s
            """,
            (source, external_id),
        ).fetchone()
        if previous is not None:
            ad_id, property_id, previous_deal, seen, active, *_ = previous
            if observed_at <= seen:
                return Receipt(
                    "ignored" if observed_at == seen else "stale", ad_id, property_id
                )
        if state == "removed":
            if previous is None:
                return Receipt("not_found")
            connection.execute(
                "UPDATE ads SET is_active=false, observed_at=%s WHERE id=%s",
                (observed_at, previous[0]),
            )
            return Receipt(
                "updated" if previous[4] else "unchanged", previous[0], previous[1]
            )
        deal = snapshot.get("deal_type")
        if previous is not None and deal in DealType.values and deal != previous[2]:
            return Receipt(
                "conflict",
                previous[0],
                previous[1],
                reason="deal_type_changed",
                previous_deal_type=cast(Deal, previous[2]),
                incoming_deal_type=cast(Deal, deal),
            )
        if not _active_valid(snapshot):
            return Receipt("rejected", reason="invalid_active_snapshot")
        active_data = cast(ActiveSnapshot, snapshot)
        references = connection.execute(
            """
            SELECT EXISTS(SELECT FROM sources WHERE code=%(source_code)s)
               AND EXISTS(SELECT FROM regions WHERE id=%(region_id)s)
               AND EXISTS(SELECT FROM property_types WHERE id=%(property_type_id)s)
            """,
            active_data,
        ).fetchone()
        if references != (True,):
            return Receipt("rejected", reason="unknown_reference")
        return _store_active(connection, active_data, previous)


def _store_active(
    connection: Connection[TupleRow], data: ActiveSnapshot, previous: TupleRow | None
) -> Receipt:
    photo_state = data["photos"]
    known_photos = photo_state["status"] == "known"
    photos = photo_state["urls"] if photo_state["status"] == "known" else []
    photo_time = data["observed_at"] if known_photos else None
    if previous is None:
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
        ad_id, property_id = previous[:2]
        changed = (
            True,
            data["url"],
            data["market_segment"],
            data["description"],
        ) != previous[4:8]
        changed |= known_photos and photos != previous[8]
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
