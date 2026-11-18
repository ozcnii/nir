"""Plain P3 data exchanged at the collection/catalog boundary."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, TypedDict

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


Snapshot = ActiveSnapshot | UnavailableSnapshot
