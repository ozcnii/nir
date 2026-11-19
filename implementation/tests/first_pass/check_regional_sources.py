"""Check that a blocked source leaves the next regional direction collectible."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.management import call_command
from playwright.sync_api import Page, Route

from app.catalog.reference_defaults import SAMARA_REGION_ID
from app.collection.browser import browser_page
from app.collection.tests.domclick_fixture import ssr

_CIAN = "https://samara.cian.ru/kupit-kvartiru-samarskaya-oblast/"
_DOMCLICK = "https://samara.domclick.ru/pokupka/kvartiry/samarskaya-oblast"
_ID = 2078968375
_CARD_PATH = f"/card/sale__flat__{_ID}"
_CARD = f"https://samara.domclick.ru{_CARD_PATH}"
_LAYOUT_PARENT = 2072470664
_LAYOUT_MEMBERS = (
    2072470552,
    2072470567,
    2072470580,
    2072470620,
    2072470627,
    2072470629,
    2072470651,
    2072470659,
    2072470683,
    2072470733,
    2072470767,
    2072470775,
    2072470782,
)


def layout_card(identifier: int, number: int, *, area: float = 54.3) -> tuple[str, str]:
    data: dict[str, object] = {
        "id": identifier,
        "address": {
            "guid": "controlled-layout-address",
            "shortDisplayName": "Гвардейский переулок, 7а",
            "locality": {"name": "Самара"},
        },
        "objectInfo": {"area": area, "rooms": 2, "floorNumber": 2},
        "building": {"floors": 17},
        "flatInfo": {"flatNumber": str(number)},
        "priceInfo": {"currentPrice": 8304840},
    }
    if identifier == _LAYOUT_PARENT:
        data["flatsWithSameLayout"] = [{"flatId": key} for key in _LAYOUT_MEMBERS]
    state = {
        "shell": {"isDeletedFlat": False, "flatDetails": {"data": data}},
        "regions": {
            "guid": "controlled-layout-address",
            "regionName": "Самарская область",
        },
    }
    url = f"https://samara.domclick.ru/card/sale__new_flat__{identifier}?to_flats=true"
    return url, f"<script>{ssr(state)}</script>"


def verify_regional_sources() -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: the published layout keys with controlled card details, an
    # ordinary listing, a separate recommendation and a blocked first source.
    layout = dict(
        layout_card(identifier, number)
        for number, identifier in enumerate((_LAYOUT_PARENT, *_LAYOUT_MEMBERS), start=1)
    )
    parent_url = next(iter(layout))
    search = {
        "status": "loaded",
        "filters": {
            "applied": {
                "deal_type": "sale",
                "category": "living",
                "offer_type": ["flat", "new_flat"],
                "aids": [4987],
            },
        },
        "pagination": {"offset": 0, "limit": 20},
        "snippetsCount": 2,
        "offersCount": 15,
    }
    card = {
        "id": _ID,
        "status": 2,
        "offerType": "flat",
        "dealType": "sale",
        "address": {
            "parents": [
                {"kind": "locality", "name": "Самара"},
                {"kind": "province", "subkind": "region", "name": "Самарская область"},
            ],
        },
        "objectInfo": {"area": "40.00", "rooms": 1},
        "priceInfo": {"currency": "RUB", "price": 6990000},
    }
    listing = (
        f"<script>{ssr({'search': search})}</script>"
        f"<main><div><a href='{_CARD_PATH}'>Квартира</a></div>"
        f"<div><a href='{parent_url}'>Планировка</a></div>"
        "<div>Сейчас это все объявления по вашему запросу</div></main>"
        "<aside><a href='/card/sale__flat__2078968376'>Рекомендуем</a></aside>"
    )
    documents = {
        _DOMCLICK: listing,
        _CARD: f"<script>{ssr({'productCard': card})}</script>",
        **layout,
    }
    requests: list[str] = []

    def respond(request: Route) -> None:
        url = request.request.url
        requests.append(url)
        blocked = url == _CIAN
        request.fulfill(
            status=403 if blocked else 200 if url in documents else 404,
            content_type="text/html; charset=utf-8",
            body=documents.get(url, "Unavailable"),
        )

    @contextmanager
    def routed_browser() -> Iterator[Page]:
        with browser_page() as page:
            page.context.route("**/*", respond)
            yield page

    # Act
    outputs = (StringIO(), StringIO())
    with patch("app.collection.search.browser_page", routed_browser):
        for attempt, stdout in enumerate(outputs):
            if attempt == 1:
                documents[parent_url] = layout_card(_LAYOUT_PARENT, 1, area=-1)[1]
            call_command(
                "collect",
                "--source",
                "cian",
                "--source",
                "domclick",
                "--region",
                str(SAMARA_REGION_ID),
                "--property-type",
                "apartment",
                "--deal",
                "sale",
                stdout=stdout,
            )

    # Assert: the next source runs and opens the parent only once per run.
    first, second = (json.loads(output.getvalue())["sources"] for output in outputs)
    assert first["cian"]["issues"] == {"http_403": 1}
    assert first["cian"]["listing_complete"] is False
    assert first["domclick"]["listing_complete"] is True, (first, requests)
    assert first["domclick"]["results"]["created"] == 15
    assert second["domclick"]["results"]["unchanged"] == 14
    assert second["domclick"]["skipped_cards"][urlsplit(parent_url).path] == (
        "number_out_of_range"
    )
    assert requests[:2] == [_CIAN, _DOMCLICK]
    assert requests.count(_CARD) == 2
    assert not any("2078968376" in url for url in requests)
    assert all(requests.count(url) == 2 for url in layout)
    assert (
        Ad.objects.filter(
            source_id="domclick",
            external_id__in=[
                f"/card/sale__new_flat__{key}"
                for key in (_LAYOUT_PARENT, *_LAYOUT_MEMBERS)
            ],
        ).count()
        == 14
    )
    saved = Ad.objects.get(source_id="domclick", external_id=_CARD_PATH)
    assert saved.property.region_id == SAMARA_REGION_ID and saved.is_active
    assert list(
        PriceHistory.objects.filter(ad=saved).values_list("price_rub", flat=True)
    ) == [
        Decimal("6990000.00"),
    ]
