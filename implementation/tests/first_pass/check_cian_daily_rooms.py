"""Exercise direct Cian pagination in Chromium against app-role PostgreSQL."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from playwright.sync_api import Page, Route

from app.catalog.reference_defaults import ROOM_TYPE_ID, SAMARA_REGION_ID
from app.collection.browser import browser_page
from app.collection.normalization import as_object
from app.collection.tests.cian_fixture import native_case, projection_html
from app.collection.tests.test_cian_partner import partner_card

_HOST = "https://samara.cian.ru"
_REGIONAL = f"{_HOST}/snyat-komnatu-posutochno-samarskaya-oblast/"
_MAP = "https://api.cian.ru/dailyrent/web/v1/map/"
_SEARCH = "https://api.cian.ru/dailyrent/web/v1/search/"
_NATIVE, _MISSING, _PARTNER = 295679032, 331646643, 335828022
_RECOMMENDED = "https://cian.sutochno.ru/detail/1649092"
_MISSING_URL = f"{_HOST}/rent/flat/{_MISSING}/"
_FILLER = tuple(range(930632001, 930632028))
_FILLER_URLS = [f"{_HOST}/rent/flat/{key}/" for key in _FILLER]
_SCOPE = {
    "_type": "flatrent",
    "engine_version": {"type": "term", "value": 2},
    "region": {"type": "terms", "value": [4608]},
    "room": {"type": "terms", "value": [0]},
    "for_day": {"type": "term", "value": "1"},
}
_QUERY_STRING = (
    "deal_type=rent&engine_version=2&offer_type=flat&region=4608&room0=1&type=2"
)


def verify_cian_daily_rooms() -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: saved ordinary room plus controlled pagination of the observed API.
    url, offer, product = native_case("day_room")
    partner_url, partner_data, _ = partner_card(
        1761933, name="Стандарт двухместный", hotel={"id": 5}, amount=3200
    )
    html_by_url = {
        url: projection_html(offer, product, "от 6 990 ₽/сут."),
        partner_url: f'<script id="__NUXT_DATA__" type="application/json">{partner_data[0]}</script>',
    }
    offers = {
        key: {
            "id": key,
            "cianId": key,
            "category": "dailyRoomRent",
            "dealType": "rent",
            "offerType": "flat",
            **(
                {"isCianPartner": True, "externalOfferUrl": partner_url}
                if key == _PARTNER
                else {}
            ),
        }
        for key in (_NATIVE, _MISSING, _PARTNER, *_FILLER)
    }
    first_map = {
        "jsonQuery": {**_SCOPE, "page": {"type": "term", "value": 1}},
        "bbox": {},
        "queryString": _QUERY_STRING,
        "zoom": 8,
        "subdomain": "samara",
    }
    document = (
        f'<aside><a href="{_RECOMMENDED}">Рекомендация</a></aside>'
        f"<script>fetch({json.dumps(_MAP)},{{method:'POST',headers:"
        "{'Content-Type':'application/json'},"
        f"body:JSON.stringify({json.dumps(first_map)})}});</script>"
    )
    mode = "complete"
    pages: list[int] = []
    searches: list[tuple[int, ...]] = []
    cards: list[str] = []
    cors = {
        "Access-Control-Allow-Origin": _HOST,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    def serve(request: Route) -> None:
        address = request.request.url
        if address == _REGIONAL:
            request.fulfill(status=200, content_type="text/html", body=document)
            return
        if address not in (_MAP, _SEARCH):
            cards.append(address)
            request.fulfill(
                status=200 if address in html_by_url else 404,
                content_type="text/html; charset=utf-8",
                body=html_by_url.get(address, "Unavailable"),
            )
            return
        if request.request.method == "OPTIONS":
            request.fulfill(status=204, headers=cors)
            return
        assert request.request.method == "POST"
        query = as_object(as_object(request.request.post_data_json)["jsonQuery"])
        if address == _MAP:
            number = as_object(query.pop("page"))["value"]
            assert type(number) is int and query == _SCOPE
            pages.append(number)
            if number == 2 and mode == "denied":
                request.fulfill(status=403, headers=cors)
                return
            if number == 2 and mode == "network":
                request.abort("connectionreset")
                return
            selection = (
                {1: [_NATIVE, *_FILLER], 2: [_NATIVE, *_FILLER], 3: [_PARTNER]}
                if mode == "complete"
                else {1: [_MISSING, *_FILLER], 2: [_PARTNER]}
            )
            scope = dict(_SCOPE)
            if number == 2 and mode == "drift":
                scope.pop("region")
                scope.pop("room")
            if number == 2 and mode == "cycle":
                scope["page"] = {"type": "term", "value": 1}
            ids = selection[number]
            request.fulfill(
                status=200,
                headers=cors,
                json={
                    "jsonQuery": scope,
                    "queryString": _QUERY_STRING,
                    "sortedOfferIds": ids,
                    "offersCount": len(ids),
                },
            )
        else:
            keys = as_object(query.pop("ids"))["value"]
            assert isinstance(keys, list) and query == _SCOPE
            assert all(type(key) is int for key in keys)
            searches.append(tuple(keys))
            request.fulfill(
                status=200,
                headers=cors,
                json={
                    "jsonQuery": _SCOPE,
                    "offers": [offers[key] for key in keys],
                    "mlSuggestions": [{"externalOfferUrl": _RECOMMENDED}],
                },
            )

    @contextmanager
    def routed_browser() -> Iterator[Page]:
        with browser_page() as page:
            page.context.route("**/*", serve)
            yield page

    def collect() -> dict[str, object]:
        output = StringIO()
        with patch("app.collection.search.browser_page", routed_browser):
            call_command(
                "collect",
                "--source",
                "cian",
                "--region",
                str(SAMARA_REGION_ID),
                "--property-type",
                "room",
                "--deal",
                "day_rent",
                stdout=output,
            )
        result: dict[str, object] = json.loads(output.getvalue())
        return result

    # Act
    result = collect()

    # Assert
    assert pages == [1, 2, 3] and searches == [
        (_NATIVE, *_FILLER),
        (_NATIVE, *_FILLER),
        (_PARTNER,),
    ]
    assert cards == [url, *_FILLER_URLS, partner_url], cards
    assert result["listing_complete"] is True and result["pages"] == 3, result
    assert result["results"] == {"created": 1}, result
    assert result["skipped_cards"] == {
        **dict.fromkeys(map(str, _FILLER), "http_404"),
        "/detail/1761933": "ambiguous_partner_unit",
    }, result
    assert not Ad.objects.filter(
        source_id="cian", external_id="/detail/1761933"
    ).exists()
    saved = Ad.objects.get(source_id="cian", external_id=str(_NATIVE))
    assert saved.property.region_id == SAMARA_REGION_ID and saved.is_active
    assert (
        saved.property.property_type_id == ROOM_TYPE_ID
        and saved.deal_type == "day_rent"
    )
    assert saved.property.area_m2 == Decimal("60.00") and saved.property.rooms == 1
    observed = saved.observed_at
    prices = PriceHistory.objects.filter(ad=saved)
    assert list(prices.values_list("price_rub", "price_kind")) == [
        (Decimal("6990.00"), "from")
    ]

    for scenario, reason in (
        ("drift", "listing_scope_changed"),
        ("cycle", "pagination_cycle"),
        ("denied", "http_403"),
        ("network", "browser_read_failed"),
    ):
        # Arrange
        mode = scenario
        pages.clear()
        searches.clear()
        cards.clear()

        # Act
        result = collect()

        # Assert
        assert pages == ([1, 2, 2] if scenario == "network" else [1, 2])
        assert searches == [(_MISSING, *_FILLER)]
        assert cards == [_MISSING_URL, *_FILLER_URLS], cards
        assert result["listing_complete"] is False
        assert result["issues"] == {"http_404": 28, reason: 1}, result
        saved.refresh_from_db()
        assert saved.is_active and saved.observed_at == observed and prices.count() == 1

    print(
        "Cian daily rooms: direct pages, ordinary room and scope guards verified in PostgreSQL"
    )
