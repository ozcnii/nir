"""Replay the observed card families through collect and the restricted app role."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from typing import cast
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from playwright.sync_api import Page, Route

from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID as FLAT,
)
from app.catalog.reference_defaults import (
    HOUSE_TYPE_ID,
    ROOM_TYPE_ID,
)
from app.collection.browser import browser_page
from app.collection.tests.avito_fixture import projection as avito_projection
from app.collection.tests.avito_fixture import projection_html as avito_html
from app.collection.tests.cian_fixture import FIXTURE, projection_html
from app.collection.tests.domclick_fixture import (
    DAILY,
    DAILY_PHOTO,
    PARTNER,
    daily_projection,
    daily_ssr,
    nuxt_script,
    partner_projection,
    ssr,
)
from app.collection.tests.test_cian_partner import PARTNER_CASES, partner_card


@dataclass(frozen=True)
class SavedCard:
    source: str
    key: str
    url: str
    html: str
    deal: str
    property_type: int
    area: str
    rooms: int | None
    price: tuple[int, str] | None
    segment: str | None = None


def _avito_cards() -> Iterator[SavedCard]:
    for family, deal, type_id, area, rooms, amount, segment in (
        ("new_build", "sale", FLAT, "48.10", 2, 14766700, "new_build"),
        ("house", "sale", HOUSE_TYPE_ID, "59.20", 2, 6250000, None),
        ("month_rent", "month_rent", FLAT, "64.00", 2, 43000, None),
        ("day_rent", "day_rent", FLAT, "42.00", 1, None, None),
    ):
        card = avito_projection(family)
        key = card["url"].rsplit("_", 1)[-1]
        yield SavedCard(
            "avito",
            key,
            card["url"],
            avito_html(card),
            deal,
            type_id,
            area,
            rooms,
            (amount, "listed") if amount is not None else None,
            segment,
        )


def _cian_observation(label: str) -> tuple[str, dict[str, object], str]:
    cases = cast(
        list[dict[str, object]],
        json.loads(FIXTURE.with_name("cian-native-directions.json").read_text())[
            "cases"
        ],
    )
    case = next(value for value in cases if value["case"] == label)
    url = cast(str, case["url"])
    offer = cast(dict[str, object], case["offer"])
    product = cast(dict[str, object], case["product"])
    return url, offer, projection_html(offer, product, case["offerPrice"])


def _cian_cards() -> Iterator[SavedCard]:
    for family, deal, type_id, area, rooms, amount, segment in (
        ("resale_flat", "sale", FLAT, "110.00", 3, 24999000, "resale"),
        ("sale_house", "sale", HOUSE_TYPE_ID, "90.90", None, 6200000, None),
        ("month_flat", "month_rent", FLAT, "40.00", 1, 38000, None),
        ("month_room", "month_rent", ROOM_TYPE_ID, "14.00", 1, 12000, None),
        ("month_house", "month_rent", HOUSE_TYPE_ID, "270.00", None, None, None),
        ("day_flat", "day_rent", FLAT, "32.00", 1, 3000, None),
        ("day_room", "day_rent", ROOM_TYPE_ID, "60.00", 1, 6990, None),
        ("day_house", "day_rent", HOUSE_TYPE_ID, "250.00", None, 15000, None),
    ):
        url, offer, html = _cian_observation(family)
        price = (
            None
            if amount is None
            else (amount, "from" if deal == "day_rent" else "listed")
        )
        yield SavedCard(
            "cian",
            str(offer["id"]),
            url,
            html,
            deal,
            type_id,
            area,
            rooms,
            price,
            segment,
        )


def _nuxt_page(config: list[str], products: list[str]) -> str:
    return "".join(
        f'<script id="__NUXT_DATA__" type="application/json">{value}</script>'
        for value in config
    ) + "".join(
        f'<script type="application/ld+json">{value}</script>' for value in products
    )


def _domclick_cards() -> Iterator[SavedCard]:
    # Selected public fields from the embedded-card matrix and native subtype trace.
    for identifier, kind, deal, area, rooms, amount in (
        (2080269220, "flat", "sale", "110.00", 3, 24999000),
        (2081993306, "flat", "rent", "55.00", 2, 70000),
        (2079647938, "house", "sale", "100.00", None, 4850000),
        (1885120006, "townhouse", "rent", "140.00", None, 50000),
    ):
        key = f"/card/{deal}__{kind}__{identifier}"
        town = kind == "townhouse"
        address: dict[str, object] = {
            "parents": [
                {"kind": "locality", "name": "Чапаевск" if town else "Самара"},
                {"kind": "province", "subkind": "region", "name": "Самарская область"},
            ],
        }
        if town:
            # The published component confirms the compound building number.
            address.update(
                name="Херсонская улица, 7/2",
                displayName="Чапаевск, Херсонская улица, 7/2",
            )
        state = {
            "id": identifier,
            "status": 2,
            "offerType": kind,
            "dealType": deal,
            "address": address,
            "objectInfo": {"area": area, "rooms": rooms},
            "priceInfo": {"currency": "RUB", "price": amount},
        }
        if kind == "house":
            state["land"] = {"area": "6.24", "areaUnit": "сотка"}
        yield SavedCard(
            "domclick",
            key,
            f"https://samara.domclick.ru{key}",
            f"<script>{ssr({'productCard': state})}</script>",
            "sale" if deal == "sale" else "month_rent",
            FLAT if kind == "flat" else HOUSE_TYPE_ID,
            area,
            rooms,
            (amount, "listed"),
        )

    # Published fields, with a controlled shared GUID binding the address to its region.
    url = "https://samara.domclick.ru/card/sale__new_flat__2072470551"
    state = {
        "shell": {
            "isDeletedFlat": False,
            "flatDetails": {
                "data": {
                    "id": 2072470551,
                    "address": {
                        "guid": "controlled-address-key",
                        "shortDisplayName": "Гвардейский переулок, 7а",
                        "locality": {"name": "Самара"},
                        "position": {"lat": 53.22549, "lon": 50.23738},
                    },
                    "objectInfo": {"area": 54.3, "rooms": 2, "floorNumber": 2},
                    "building": {"floors": 17},
                    "flatInfo": {"flatNumber": "121"},
                    "priceInfo": {"currentPrice": 8304840},
                    "media": {
                        "photos": [
                            "/vitrina/8b/4a/8b4a4a26dcc6a064b4f9706fa145df8474def52e.webp"
                        ]
                    },
                }
            },
        },
        "regions": {
            "guid": "controlled-address-key",
            "regionName": "Самарская область",
        },
    }
    yield SavedCard(
        "domclick",
        urlsplit(url).path,
        url,
        f"<script>{ssr(state)}</script>",
        "sale",
        FLAT,
        "54.30",
        2,
        (8304840, "listed"),
        "new_build",
    )
    yield SavedCard(
        "domclick",
        "/detail/2252883",
        PARTNER,
        _nuxt_page([nuxt_script(partner_projection())], []),
        "day_rent",
        FLAT,
        "23.00",
        1,
        None,
    )
    html = (
        f"<script>{daily_ssr(daily_projection())}</script>"
        f'<div data-e2e-id="gallery-widget"><img src="{DAILY_PHOTO}"></div>'
    )
    yield SavedCard(
        "domclick",
        urlsplit(DAILY).path,
        DAILY,
        html,
        "day_rent",
        FLAT,
        "36.00",
        1,
        None,
    )


def _collect(url: str, html: str) -> dict[str, object]:
    def serve(route: Route) -> None:
        if route.request.url.split("?", 1)[0] == url:
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            )
        else:
            route.abort()

    @contextmanager
    def local_page() -> Iterator[Page]:
        with browser_page() as page:
            page.route("**/*", serve)
            yield page

    output = StringIO()
    with patch("app.collection.one_card.browser_page", local_page):
        call_command("collect", url + "?utm_source=replay#card", stdout=output)
    return cast(dict[str, object], json.loads(output.getvalue()))


def _verify_active(case: SavedCard) -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: load one preserved family, keeping the original key.
    ads = Ad.objects.filter(source_id=case.source, external_id=case.key)

    # Act
    created = _collect(case.url, case.html)

    # Assert: the projected publication reached the catalog with its published key.
    ad = ads.get()
    assert created["outcome"] == "created", case
    assert created["ad_id"] == ad.pk, case
    assert created["property_id"] == ad.property_id, case
    assert ad.deal_type == case.deal and ad.market_segment == case.segment, case
    assert ad.property.property_type_id == case.property_type, case
    assert (
        ad.property.area_m2 == Decimal(case.area) and ad.property.rooms == case.rooms
    ), case
    expected = [(Decimal(case.price[0]), case.price[1])] if case.price else []
    assert (
        list(PriceHistory.objects.filter(ad=ad).values_list("price_rub", "price_kind"))
        == expected
    ), case
    print(
        f"{case.source} {case.key}: {case.deal}, {case.area} m², price {case.price}: verified"
    )


def _verify_no_active_write(url: str, html: str, reason: str) -> None:
    from app.catalog.models import Ad, PriceHistory, Property

    # Arrange: neither a rejected unit nor a removed unknown key adds anything.
    before = (
        Property.objects.count(),
        Ad.objects.count(),
        PriceHistory.objects.count(),
    )

    # Act
    try:
        outcome = _collect(url, html)
    except CommandError as error:
        # Assert
        assert str(error) == reason, (url, error)
    else:
        assert reason == "not_found" and outcome["outcome"] == reason, (url, outcome)

    # Assert
    assert (
        Property.objects.count(),
        Ad.objects.count(),
        PriceHistory.objects.count(),
    ) == before
    print(f"{url}: {reason}; no rows added")


def verify_saved_cards() -> None:
    for case in (*_avito_cards(), *_cian_cards(), *_domclick_cards()):
        _verify_active(case)

    url, _, html = _cian_observation("sale_room")
    _verify_no_active_write(url, html, "conflicting_room_area")
    for identifier, name, hotel_id, hotel_type, amount in PARTNER_CASES:
        url, config, products = partner_card(
            identifier,
            name=name,
            hotel={"id": hotel_id, "type": hotel_type},
            amount=amount,
        )
        _verify_no_active_write(
            url, _nuxt_page(config, products), "hotel_accommodation"
        )

    # The ordinary Cian partner room is a controlled card, using published Nuxt paths.
    url, config, products = partner_card(
        123, name="Комната рядом с парком", hotel=None, amount=5800
    )
    _verify_active(
        SavedCard(
            "cian",
            "/detail/123",
            url,
            _nuxt_page(config, products),
            "day_rent",
            ROOM_TYPE_ID,
            "13.00",
            None,
            (5800, "listed"),
        )
    )

    url, _, html = _cian_observation("removed")
    _verify_no_active_write(url, html, "not_found")
    url = "https://domclick.ru/card/sale__house__2071895305"
    marker = "<main><p>Объявление снято с размещения</p></main>"
    removed = {"id": 2071895305, "offerType": "house", "dealType": "sale", "status": 8}
    _verify_no_active_write(
        url, f"<script>{ssr({'productCard': removed})}</script>{marker}", "not_found"
    )
    detail = partner_projection()
    detail.update(type={"name": "room"}, hotel={"id": 1, "title": "Отель"})
    _verify_no_active_write(
        PARTNER, _nuxt_page([nuxt_script(detail)], []), "hotel_or_ambiguous_room"
    )
