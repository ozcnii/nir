"""Plain P3 data exchanged at the collection/catalog boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypedDict, TypeGuard, get_args
from urllib.parse import SplitResult, urlsplit

Deal = Literal["sale", "month_rent", "day_rent"]
Market = Literal["resale", "new_build"]
PriceKind = Literal["listed", "from"]


class Unavailable(TypedDict):
    status: Literal["unavailable"]


class KnownPrice(TypedDict):
    status: Literal["known"]
    price_rub: Decimal
    price_kind: PriceKind
    currency: Literal["RUB"]


class KnownPhotos(TypedDict):
    status: Literal["known"]
    urls: list[str]


class SnapshotKey(TypedDict):
    source_code: str
    external_id: str
    observed_at: datetime


class PropertyFacts(TypedDict):
    address: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    area_m2: Decimal | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    land_area_m2: Decimal | None


PROPERTY_FACT_FIELDS = frozenset(PropertyFacts.__annotations__)


class ActiveSnapshot(SnapshotKey, PropertyFacts):
    state: Literal["active"]
    url: str
    region_id: int
    property_type_id: int
    deal_type: Deal
    market_segment: Market | None
    description: str | None
    price: KnownPrice | Unavailable
    photos: KnownPhotos | Unavailable


class RemovedSnapshot(SnapshotKey):
    state: Literal["removed"]
    url: str
    deal_type: Deal


class UnavailableSnapshot(SnapshotKey):
    state: Literal["unavailable"]


Snapshot = ActiveSnapshot | RemovedSnapshot | UnavailableSnapshot


SnapshotOutcome = Literal[
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


@dataclass(frozen=True)
class SnapshotResult:
    outcome: SnapshotOutcome
    ad_id: int | None = None
    property_id: int | None = None
    price_changed: bool = False
    reason: str | None = None
    previous_deal_type: Deal | None = None
    incoming_deal_type: Deal | None = None


def is_database_text(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        return b"\x00" not in value.encode()
    except UnicodeEncodeError:
        return False


def _parse_http_url(value: object) -> SplitResult | None:
    if not is_database_text(value) or any(
        char.isspace() or ord(char) < 32 for char in value
    ):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme in ("http", "https")
            and url.hostname
            and url.username is None
            and url.port != 0
        ):
            return url
    except ValueError:
        return None
    return None


def is_publication_url(value: object) -> bool:
    return _parse_http_url(value) is not None


def is_photo_url(value: object) -> bool:
    url = _parse_http_url(value)
    if url is None:
        return False
    host = url.hostname or ""
    if ":" in host:
        return "%" not in host
    return all(
        label.replace("-", "a").isalnum() and label == label.strip("-")
        for label in host.removesuffix(".").split(".")
    )


def is_nonblank_text(value: object) -> TypeGuard[str]:
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
    if not data.keys() >= PROPERTY_FACT_FIELDS:
        return False
    if data["address"] is not None and not is_nonblank_text(data["address"]):
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


def has_active_payload(data: Mapping[str, object]) -> TypeGuard[ActiveSnapshot]:
    """Check the active fields after the snapshot key has been checked."""
    if not {"description", "market_segment", "price", "photos"} <= data.keys():
        return False
    if not is_publication_url(data.get("url")) or data.get("deal_type") not in get_args(
        Deal
    ):
        return False
    segment = data["market_segment"]
    if segment is not None and segment not in get_args(Market):
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
        and price.get("price_kind") in get_args(PriceKind)
        and _decimal_fits_column(price.get("price_rub"), "0", "99999999999999.99", 2)
    )
    urls = photos.get("urls")
    photos_ok = photos.get("status") == "unavailable" or (
        photos.get("status") == "known"
        and isinstance(urls, list)
        and all(is_publication_url(url) for url in urls)
        and len(set(urls)) == len(urls)
    )
    return price_ok and photos_ok
