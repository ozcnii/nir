"""Selected published Domclick fields and controlled replay fixtures."""

import json
from datetime import UTC, datetime

OBSERVED = datetime(2026, 11, 18, 13, tzinfo=UTC)
ROOM = "https://samara.domclick.ru/card/sale__room__2071752356"
HOUSE = "https://samara.domclick.ru/card/sale__house__2079647938"
PARTNER = "https://domclick.sutochno.ru/detail/2252883"
DAILY = "https://24.domclick.ru/card/1999c326-4f66-410d-8ea7-0d01a6e8af4e"
LISTING = "https://24.domclick.ru/samara"
DAILY_ASSET = "/homestay/60/a6/60a62c62f1ea470a894f49b660d12acc"
DAILY_PHOTO = f"https://img.dmclk.ru/s1200x800q80{DAILY_ASSET}.webp"


def native_proposal() -> dict[str, object]:
    # Selected paths from field-audit/rooms62 and the embedded card matrix.
    return {
        "id": 2071752356,
        "offerType": "room",
        "dealType": "sale",
        "status": 2,
        "roomsOffered": 1,
        "address": {
            "displayName": "Россия, Самара, Солнечная улица, 7",
            "parents": [
                {"kind": "locality", "name": "Самара"},
                {"kind": "province", "subkind": "region", "name": "Самарская область"},
                {"kind": "province", "name": "Приволжский федеральный округ"},
            ],
            "position": {"lat": 53.240691, "lon": 50.190295},
        },
        "house": {"info": {"floors": 9}},
        "objectInfo": {
            "area": 68.8,
            "roomArea": 9.2,
            "rooms": 3,
            "floor": 1,
            "description": "Текст продавца: undefined — сохранить.\nРазмер 9,2 м².",
        },
        "priceInfo": {"currency": "RUB", "price": 1400000},
    }


def partner_projection() -> dict[str, object]:
    # Selected public paths from the live 2252883 card.
    return {
        "id": 2252883,
        "url": "https://sutochno.ru/front/searchapp/detail/2252883",
        "status": {"name": "approved"},
        "type": {"name": "apartment"},
        "title": "1-комнатная квартира",
        "hotel": None,
        "location": {
            "city": {"id": 366640, "title": "Новокуйбышевск"},
            "address": "Суворова улица, 13А",
            "lat": 53.108529999998,
            "lng": 49.949734,
        },
        "media": {
            "images": [
                {
                    "path": "//static.sutochno.ru/doc/files/objects/2/252/883/699d88d60d929.jpg"
                }
            ]
        },
        "propertyGroups": [
            {
                "name": "basic_properties",
                "properties": [
                    {"name": "rooms", "value": 1},
                    {"name": "area", "title": "Площадь (м2)", "value": 23},
                    {"name": "floor", "value": 4},
                    {"name": "max_floor", "value": 5},
                ],
            },
            {
                "name": "detail_description",
                "properties": [
                    {"name": "description", "value": "Описание продавца.\n23 м²."},
                ],
            },
        ],
    }


def nuxt_script(detail: dict[str, object]) -> str:
    entries: list[object] = [["ShallowReactive", 1]]

    def encode(value: object) -> int:
        index = len(entries)
        entries.append(None)
        if isinstance(value, dict):
            entries[index] = {key: encode(member) for key, member in value.items()}
        elif isinstance(value, list):
            entries[index] = [encode(member) for member in value]
        else:
            entries[index] = value
        return index

    encode({"data": {"detail:2252883": detail}})
    return json.dumps(entries)


def daily_projection() -> dict[str, object]:
    # Selected public paths from field-audit/domclick64-card-fields.json and live DOM.
    return {
        "uid": "0f993e54-dbfc-4633-bf0a-4224738136b3",
        "status": "published",
        "category": "flat",
        "geoInfo": {
            "address": "Самарская область, пос. Придорожный, Николаевский проспект, 31",
            "lat": 53.112009,
            "lon": 50.145113,
        },
        "area": 36,
        "areaUnit": "м²",
        "rooms": 1,
        "floor": 6,
        "floorsMax": 9,
        "photos": [{"url": f"{DAILY_ASSET}.jpg"}],
        "basePrice": 0,
    }


def ssr(source: dict[str, object]) -> str:
    return "window.__SSR_STATE__ = " + json.dumps(source) + ";"


def daily_ssr(data: dict[str, object]) -> str:
    return ssr(
        {
            "inventory": {
                "searchData": {"id": DAILY.rsplit("/", 1)[1]},
                "fetchStatus": "success",
                "data": data,
            }
        }
    )
