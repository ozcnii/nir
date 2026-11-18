"""Convert the supported Cian payload to catalog fields."""

import logging
import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import cast

from app.catalog.contracts import (
    ActiveSnapshot,
    KnownPhotos,
    KnownPrice,
    SnapshotKey,
    Unavailable,
    is_photo_url,
)
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, SAMARA_REGION_ID

logger = logging.getLogger(__name__)

CIAN_REGION_LOCATION_TYPE = 2
CIAN_SAMARA_LOCATION_ID = 4608


class RejectedCard(ValueError):
    """A recognized card has invalid required or structural data."""


# The integer part is either space-grouped in thousands or ungrouped.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)(?:[.,][0-9]+)?")


def number(value: object) -> Decimal:
    if isinstance(value, str):
        value = value.strip().replace("\u00a0", " ").replace("\u202f", " ")
        if not _DECIMAL_TEXT.fullmatch(value):
            raise RejectedCard("invalid_number")
        value = value.replace(" ", "").replace(",", ".")
    elif isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise RejectedCard("invalid_number")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise RejectedCard("invalid_number") from error
    if not result.is_finite():
        raise RejectedCard("invalid_number")
    return result


def decimal_2(value: object, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    result = number(value)
    if not minimum <= result < maximum:
        raise RejectedCard("number_out_of_range")
    rounded = result.quantize(Decimal("0.01"))
    if rounded != result:
        raise RejectedCard("invalid_precision")
    return rounded


def coordinate(value: object, limit: int) -> Decimal:
    result = number(value)
    if not -limit <= result <= limit:
        raise RejectedCard("coordinate_out_of_range")
    return result.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_EVEN)


def optional_integer(value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    result = number(value)
    if not minimum <= result <= 32767 or result != result.to_integral_value():
        raise RejectedCard("invalid_integer")
    return int(result)


def as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _optional_object(value: object) -> dict[str, object]:
    if value is not None and not isinstance(value, dict):
        raise RejectedCard("invalid_structure")
    return as_object(value)


def _price(
    product: dict[str, object], display: object, offer_data: dict[str, object]
) -> KnownPrice | Unavailable:
    offer = as_object(product.get("offers"))
    try:
        if offer.get("@type") != "Offer":
            raise RejectedCard("missing_offer_price")
        if offer.get("priceCurrency") != "RUB":
            raise RejectedCard("price_not_rub")
        price = decimal_2(
            offer.get("price"), minimum=Decimal(0), maximum=Decimal("1e14")
        )
        stated = as_object(offer_data.get("bargainTerms")).get("price")
        if (
            stated is not None
            and decimal_2(stated, minimum=Decimal(0), maximum=Decimal("1e14")) != price
        ):
            raise RejectedCard("price_fields_disagree")
    except RejectedCard as error:
        logger.warning("cian/%s price unavailable: %s", offer_data["id"], error)
        return {"status": "unavailable"}
    return {
        "status": "known",
        "price_rub": price,
        "price_kind": "from"
        if isinstance(display, str)
        and display.casefold().split(maxsplit=1)[:1] == ["от"]
        else "listed",
        "currency": "RUB",
    }


def _photos(
    product: dict[str, object], offer: dict[str, object]
) -> KnownPhotos | Unavailable:
    published = offer.get("photos")
    images = product.get("image")
    if published == [] and images is None:
        return {"status": "known", "urls": []}
    if (
        not isinstance(images, list)
        or not isinstance(published, list)
        or len(published) != len(images)
        or not all(is_photo_url(url) for url in cast(list[object], images))
    ):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], images)))}


def normalize_cian_card(
    url: str, key: SnapshotKey, data: dict[str, object], product: dict[str, object]
) -> ActiveSnapshot:
    offer = as_object(data.get("offer"))
    if (
        type(offer.get("id")) is not int
        or str(offer["id"]) != key["external_id"]
        or offer.get("cianId", offer["id"]) != offer["id"]
        or offer.get("offerType") != "flat"
        or offer.get("dealType") != "sale"
        or offer.get("category") != "newBuildingFlatSale"
        or offer.get("status") != "published"
    ):
        raise RejectedCard("invalid_card_direction")

    geo = as_object(offer.get("geo"))
    address = geo.get("address")
    if not isinstance(address, list):
        raise RejectedCard("invalid_region")
    components = [as_object(item) for item in cast(list[object], address)]
    subjects = [
        part
        for part in components
        if part.get("type") == "location"
        and part.get("locationTypeId") == CIAN_REGION_LOCATION_TYPE
    ]
    if (
        len(subjects) != 1
        or subjects[0].get("id") != CIAN_SAMARA_LOCATION_ID
        or subjects[0].get("fullName") != "Самарская область"
    ):
        raise RejectedCard("invalid_region")
    names = []
    for part in components:
        name = part.get("fullName")
        if not isinstance(name, str) or not name.strip():
            raise RejectedCard("invalid_address")
        names.append(" ".join(name.split()))

    coordinates = _optional_object(geo.get("coordinates"))
    latitude, longitude = coordinates.get("lat"), coordinates.get("lng")
    if (latitude is None) != (longitude is None):
        raise RejectedCard("invalid_coordinate_pair")

    area = offer.get("totalArea")
    floor = optional_integer(offer.get("floorNumber"), minimum=-32768)
    floors = optional_integer(
        _optional_object(offer.get("building")).get("floorsCount"), minimum=1
    )
    if floor is not None and floors is not None and floor > floors:
        raise RejectedCard("floor_above_building")
    rooms = optional_integer(offer.get("roomsCount"), minimum=0)
    if rooms == 0:
        raise RejectedCard("unconfirmed_studio")
    description = offer.get("description")
    if description is not None and not isinstance(description, str):
        raise RejectedCard("invalid_description")

    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": SAMARA_REGION_ID,
        "property_type_id": APARTMENT_TYPE_ID,
        "deal_type": "sale",
        "market_segment": "new_build",
        "address": ", ".join(names),
        "latitude": coordinate(latitude, 90) if latitude is not None else None,
        "longitude": coordinate(longitude, 180) if longitude is not None else None,
        "area_m2": decimal_2(area, minimum=Decimal("0.01"), maximum=Decimal("1e10"))
        if area is not None
        else None,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": None,
        "description": description or None,
        "price": _price(product, data.get("offerPrice"), offer),
        "photos": _photos(product, offer),
    }
