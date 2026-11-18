"""Plain P3 data exchanged at the collection/catalog boundary."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, TypedDict
from urllib.parse import urlsplit

Deal = Literal["sale", "month_rent", "day_rent"]
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


class UnavailableSnapshot(SnapshotKey):
    state: Literal["unavailable"]


class ActiveSnapshot(SnapshotKey):
    state: Literal["active"]
    url: str
    region_id: int
    property_type_id: int
    deal_type: Deal
    market_segment: Literal["resale", "new_build"] | None
    address: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    area_m2: Decimal | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    land_area_m2: Decimal | None
    description: str | None
    price: KnownPrice | Unavailable
    photos: KnownPhotos | Unavailable


class RemovedSnapshot(SnapshotKey):
    state: Literal["removed"]


Snapshot = ActiveSnapshot | RemovedSnapshot | UnavailableSnapshot


def is_http_url(value: object) -> bool:
    if not isinstance(value, str) or any(
        char.isspace() or ord(char) < 32 for char in value
    ):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme in ("http", "https")
            and bool(url.hostname)
            and url.username is None
        )
    except ValueError:
        return False
