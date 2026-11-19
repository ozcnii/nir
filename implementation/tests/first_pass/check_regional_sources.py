"""Check that a blocked source leaves the next regional direction collectible."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

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


def verify_regional_sources() -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: replay the saved native state and one main result, with a separate
    # recommendation and a blocked first source.
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
        "snippetsCount": 1,
        "offersCount": 1,
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
        "<div>Сейчас это все объявления по вашему запросу</div></main>"
        "<aside><a href='/card/sale__flat__2078968376'>Рекомендуем</a></aside>"
    )
    documents = {
        _DOMCLICK: listing,
        _CARD: f"<script>{ssr({'productCard': card})}</script>",
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
        for stdout in outputs:
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

    # Assert: the next source runs after the refusal, with one ad and one price.
    first, second = (json.loads(output.getvalue())["sources"] for output in outputs)
    assert first["cian"]["issues"] == {"http_403": 1}
    assert first["cian"]["listing_complete"] is False
    assert first["domclick"]["listing_complete"] is True, (first, requests)
    assert first["domclick"]["results"]["created"] == 1
    assert second["domclick"]["results"]["unchanged"] >= 1
    assert requests[:3] == [_CIAN, _DOMCLICK, _CARD]
    assert requests.count(_CARD) == 2
    assert not any("2078968376" in url for url in requests)
    saved = Ad.objects.get(source_id="domclick", external_id=_CARD_PATH)
    assert saved.property.region_id == SAMARA_REGION_ID and saved.is_active
    assert list(
        PriceHistory.objects.filter(ad=saved).values_list("price_rub", flat=True)
    ) == [
        Decimal("6990000.00"),
    ]
