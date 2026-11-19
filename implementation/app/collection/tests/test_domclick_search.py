import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from unittest import TestCase

from playwright.sync_api import Page, Route, sync_playwright

from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    SAMARA_REGION_ID,
)
from app.collection.collectors.domclick_search import (
    SearchRoute,
    advance,
    read_group,
    read_page,
    route_for,
    routes_for,
)
from app.collection.normalization import RejectedCard
from app.collection.tests.domclick_fixture import DAILY, daily_listing_parts, ssr

_NATIVE = "https://samara.domclick.ru"
_GROUP = f"{_NATIVE}/card/sale__new_flat__2072470664?to_flats=true"


@contextmanager
def _page(
    url: str, document: str, *, reply: tuple[str, int, dict[str, object]] | None = None
) -> Iterator[Page]:
    def serve(route: Route) -> None:
        if route.request.url == url:
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=document
            )
        elif reply is not None and route.request.url == reply[0]:
            route.fulfill(status=reply[1], json=reply[2])
        else:
            route.abort()

    with (
        sync_playwright() as playwright,
        playwright.chromium.launch(channel="chromium", headless=True) as browser,
        browser.new_context(service_workers="block") as context,
    ):
        page = context.new_page()
        page.route("**/*", serve)
        page.goto(url)
        yield page


def _native_route() -> SearchRoute:
    route = route_for(SAMARA_REGION_ID, "sale", APARTMENT_TYPE_ID)
    assert route is not None
    return route


def _native_document(
    body: str, *, count: int, region: int = 4987, status: str = "loaded"
) -> str:
    state = {
        "status": status,
        "pagination": {"offset": 0, "limit": 20},
        "snippetsCount": count,
        "filters": {
            "applied": {
                "aids": [region],
                "deal_type": "sale",
                "category": "living",
                "offer_type": ["flat", "new_flat"],
            }
        },
    }
    return f"<script>{ssr({'search': state})}</script>{body}"


class DomclickSearchTests(TestCase):
    def test_house_routes_keep_the_subject_and_include_disjoint_subtypes(self) -> None:
        # Arrange
        region = SAMARA_REGION_ID

        # Act
        routes = routes_for(region, "month_rent", HOUSE_TYPE_ID)
        unsupported = routes_for(56, "sale", HOUSE_TYPE_ID)

        # Assert
        self.assertEqual(
            [route.kinds for route in routes],
            [
                ("house",),
                ("house_part",),
                ("townhouse",),
            ],
        )
        self.assertTrue(all("aids=4987" in route.start for route in routes))
        self.assertEqual(unsupported, ())

    def test_native_filters_and_paginator_bound_the_primary_cards(self) -> None:
        # Arrange: the published mixed-apartment scope and group link.
        route = _native_route()
        flat = f"{_NATIVE}/card/sale__flat__2078968375"
        excluded = f"{_NATIVE}/card/sale__flat__2080269220"
        html = _native_document(
            f'<aside><a href="{excluded}">Похожая карточка</a></aside>'
            f'<div><section><a href="{flat}">Квартира</a>'
            f'<a href="{_GROUP}">14 предложений такой планировки</a></section>'
            '<div class="pgnt-root-example">'
            '<a aria-label="Следующая" href="?page=2">Следующая</a>'
            "</div></div>",
            count=2,
        )

        # Act
        with _page(route.start, html) as page:
            result = read_page(page, route, time.monotonic() + 5)

        # Assert
        self.assertEqual(
            result.urls,
            {
                "/card/sale__flat__2078968375": flat,
                "/card/sale__new_flat__2072470664": _GROUP,
            },
        )
        self.assertEqual(result.group_urls, (_GROUP,))
        self.assertEqual(result.next_url, route.start + "?page=2")
        self.assertEqual(result.incomplete, ())

    def test_native_empty_requires_the_ready_marker_and_effective_region(self) -> None:
        # Arrange: explicit empty results can coexist with real recommendation links.
        route = _native_route()
        body = (
            "<div><p>Поиск не дал результатов</p>"
            f'<a href="{_GROUP}">Похожее объявление</a></div>'
        )
        for region in (4987, 5007):
            with self.subTest(region=region):
                html = _native_document(body, count=0, region=region)
                with _page(route.start, html) as page:
                    if region == 4987:
                        # Act
                        result = read_page(page, route, time.monotonic() + 5)

                        # Assert
                        self.assertEqual(result.urls, {})
                        self.assertEqual(result.incomplete, ())
                    else:
                        # Act / Assert
                        with self.assertRaisesRegex(
                            RejectedCard, "^listing_scope_changed$"
                        ):
                            read_page(page, route, time.monotonic() + 5)

    def test_layout_children_use_the_published_builder_and_query(self) -> None:
        # Arrange: all 13 observed flatId values from domclick64-group-units.json.
        ids = (
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
        route = _native_route()
        data = {"id": 2072470664, "flatsWithSameLayout": [{"flatId": i} for i in ids]}
        html = f"<script>{ssr({'shell': {'flatDetails': {'data': data}}})}</script>"

        # Act
        with _page(_GROUP, html) as page:
            result = read_group(page, route, time.monotonic() + 5, group_url=_GROUP)

        # Assert
        self.assertEqual(
            result.urls,
            {
                f"/card/sale__new_flat__{i}": f"{_NATIVE}/card/sale__new_flat__{i}?to_flats=true"
                for i in ids
            },
        )
        self.assertEqual(result.incomplete, ())

    def test_daily_primary_quote_uses_a_click_to_preserve_regional_context(
        self,
    ) -> None:
        # Arrange: combined recorded site structures for the same visible UUID.
        route = route_for(SAMARA_REGION_ID, "day_rent", APARTMENT_TYPE_ID)
        assert route is not None
        identifier = DAILY.rsplit("/", 1)[1]
        controls, primary = daily_listing_parts()
        hidden_id = "eb807dc5-d00e-4731-9197-94e997c275f0"
        ids = [identifier.upper(), hidden_id]
        entities = {
            key: {"id": key, "category": "flat", "source": "CAS"} for key in ids
        }
        controls = controls.replace(
            '"searchData": {"adt": 1}',
            f'"searchData": {{"adt": 1, "page": 1, "locationId": "{route.source_region}"}}'
            f', "total": 37, "entities": {json.dumps(entities)}',
        ).replace(f'"ids": ["{identifier.upper()}"]', f'"ids": {json.dumps(ids)}')
        html = (
            f'{controls}<div id="app"><div>{primary}</div></div>'
            '<a aria-label="Следующая" href="/search?page=2">Следующая</a>'
        )

        # Act
        with _page(route.start, html) as page:
            result = read_page(page, route, time.monotonic() + 5)

        # Assert
        key = f"/card/{identifier}"
        hidden_key = f"/card/{hidden_id}"
        self.assertEqual(
            result.urls,
            {
                key: DAILY,
                hidden_key: "https://24.domclick.ru" + hidden_key,
            },
        )
        self.assertEqual(result.incomplete, ())
        self.assertNotIn(hidden_key, result.quotes)
        self.assertEqual(result.next_control, 'a[aria-label="Следующая"]')
        self.assertIsNone(result.next_url)
        self.assertEqual(result.quotes[key].amount, "3 910 ₽")
        self.assertEqual(
            Decimal(result.quotes[key].amount.split(" ₽")[0].replace(" ", "")),
            Decimal(3910),
        )

    def test_daily_click_observes_ready_response_while_inline_page_is_stale(
        self,
    ) -> None:
        # Arrange: passive reply for offset 36, keeping the first inline state.
        route = route_for(SAMARA_REGION_ID, "day_rent", APARTMENT_TYPE_ID)
        assert route is not None
        identifier = DAILY.rsplit("/", 1)[1]
        reply_url = (
            "https://24.domclick.ru/api/offers/ad/v1?"
            f"locationGuid={route.source_region}&adults=1&limit=36&offset=36"
        )
        controls, primary = daily_listing_parts()
        disabled = '<button aria-label="Следующая" disabled>Следующая</button>'
        html = (
            f'{controls}<div id="app"><div>{primary}</div></div>'
            '<a class="pgnt-selected" href="/search">1</a>'
            '<a id="next" aria-label="Следующая" href="/search?page=2">Следующая</a>'
            '<script>document.querySelector("#next").addEventListener("click", async event => {'
            f"event.preventDefault(); await (await fetch({json.dumps(reply_url)})).json();"
            'history.pushState({}, "", "/search?page=2");'
            'document.querySelector(".pgnt-selected").textContent = "2";'
            f"event.target.outerHTML = {json.dumps(disabled)};"
            "});</script>"
        )
        payload: dict[str, object] = {
            "total": 37,
            "offers": [{"id": identifier, "category": "flat", "source": "CAS"}],
        }

        # Act
        with _page(route.start, html, reply=(reply_url, 200, payload)) as page:
            result = advance(page, route, 1, "#next", time.monotonic() + 5)

        # Assert
        self.assertEqual(result.number, 2)
        self.assertEqual(result.urls, {f"/card/{identifier}": DAILY})
        self.assertEqual(result.quotes[f"/card/{identifier}"].amount, "3 910 ₽")

    def test_daily_click_distinguishes_the_recorded_cycle_scope_loss_and_http_error(
        self,
    ) -> None:
        # Arrange: public response shape and recorded fallback offset 792 -> 756.
        route = route_for(SAMARA_REGION_ID, "day_rent", APARTMENT_TYPE_ID)
        assert route is not None
        for region, status, reason in (
            (route.source_region, 200, "pagination_cycle"),
            ("6369cbfc-1f06-4574-adba-82f4dc42c0f7", 200, "listing_scope_changed"),
            (route.source_region, 403, "http_403"),
        ):
            with self.subTest(region=region, status=status):
                reply_url = (
                    "https://24.domclick.ru/api/offers/ad/v1?"
                    f"locationGuid={region}&adults=1&limit=36&offset=792"
                )
                html = (
                    '<a id="next" href="/search?page=23">Следующая</a>'
                    f'<script>document.querySelector("#next").onclick = event => {{ event.preventDefault(); fetch({json.dumps(reply_url)}); }};</script>'
                )

                # Act / Assert
                with (
                    _page(
                        route.start,
                        html,
                        reply=(reply_url, status, {"total": 789, "offers": []}),
                    ) as page,
                    self.assertRaisesRegex(RejectedCard, f"^{reason}$"),
                ):
                    advance(page, route, 22, "#next", time.monotonic() + 5)
