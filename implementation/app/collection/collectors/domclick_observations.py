"""Project the public fields read by the verified Domclick normalizers."""

from app.collection.collectors.domclick_state import NuxtState
from app.collection.domclick_normalization import DailyListingQuote
from app.collection.normalization import as_object
from app.collection.observations import photo_links, public_fields, public_text, rows


def native_fields(
    data: dict[str, object],
    page_title: str,
    *,
    geography: dict[str, object] | None = None,
) -> dict[str, object]:
    address = as_object(data.get("address"))
    building = (
        data.get("building")
        if geography is not None
        else as_object(data.get("house")).get("info")
    )
    fields: dict[str, object] = {
        **public_fields(data, "status", "offerType", "dealType", "roomsOffered"),
        "title": public_text(page_title),
        "address": {
            **public_fields(
                address, "guid", text=("name", "displayName", "shortDisplayName")
            ),
            "locality": public_fields(address.get("locality"), text=("name",)),
            "position": public_fields(address.get("position"), "lat", "lon"),
            "parents": [
                public_fields(parent, "kind", "subkind", text=("name",))
                for parent in rows(address.get("parents"))
            ],
        },
        "objectInfo": public_fields(
            data.get("objectInfo"),
            "area",
            "roomArea",
            "rooms",
            "floor",
            "floorNumber",
            "flatNumber",
            text=("description",),
        ),
        "building": public_fields(building, "floors"),
        "land": public_fields(data.get("land"), "area", "areaUnit"),
        "priceInfo": public_fields(
            data.get("priceInfo"), "price", "currency", "currentPrice"
        ),
        "photos": photo_links(data.get("photos")),
    }
    if geography is not None:
        fields["photos"] = photo_links(as_object(data.get("media")).get("photos"))
        fields["flatInfo"] = public_fields(data.get("flatInfo"), "flatNumber")
        fields["region"] = public_fields(geography, "guid", text=("regionName",))
    return fields


def daily_fields(
    data: dict[str, object],
    page_title: str,
    gallery: list[str],
    quote: DailyListingQuote | None,
) -> dict[str, object]:
    return {
        **public_fields(
            data,
            "status",
            "category",
            "propertyType",
            "type",
            "area",
            "areaUnit",
            "rooms",
            "floor",
            "floorsMax",
            "basePrice",
            text=("title", "description"),
        ),
        "page_title": public_text(page_title),
        "geoInfo": public_fields(data.get("geoInfo"), "lat", "lon", text=("address",)),
        "photos": photo_links(gallery),
        "photo_assets": [
            public_fields(photo, "url") for photo in rows(data.get("photos"))
        ],
        "price_context": (
            {
                "dates": quote.dates,
                "guests": quote.guests,
                "amount": quote.amount,
                "nightly": quote.nightly,
            }
            if quote is not None
            else None
        ),
    }


def partner_fields(state: NuxtState, detail: dict[str, object]) -> dict[str, object]:
    def read(record: object, name: str) -> object:
        try:
            return state.field(record, name)
        except ValueError:
            return None

    def resolve_row(index: object) -> object:
        try:
            return state.resolve(index)
        except ValueError:
            return None

    location = read(detail, "location")
    city = read(location, "city")
    fields: dict[str, object] = {
        "type": public_fields({"type": read(read(detail, "type"), "name")}, "type"),
        "location": public_fields(
            {
                "lat": read(location, "lat"),
                "lng": read(location, "lng"),
                "city_id": read(city, "id"),
                "city": read(city, "title"),
                "address": read(location, "address"),
            },
            "lat",
            "lng",
            "city_id",
            text=("address", "city"),
        ),
        "title": public_fields({"title": read(detail, "title")}, text=("title",)),
        "photos": photo_links(
            [
                read(resolve_row(index), "path")
                for index in rows(read(read(detail, "media"), "images"))
            ]
        ),
        "attributes": [],
    }
    attributes = []
    for index in rows(read(detail, "propertyGroups")):
        group = resolve_row(index)
        group_name = read(group, "name")
        if group_name not in ("basic_properties", "detail_description"):
            continue
        for property_index in rows(read(group, "properties")):
            prop = resolve_row(property_index)
            name = read(prop, "name")
            if name not in ("area", "rooms", "floor", "max_floor", "description"):
                continue
            value = {
                "group": group_name,
                "name": name,
                "title": read(prop, "title"),
                "value": read(prop, "value"),
            }
            attributes.append(
                public_fields(value, "group", "name", "title", text=("value",))
                if name == "description"
                else public_fields(value, "group", "name", "title", "value")
            )
    fields["attributes"] = attributes
    return fields
