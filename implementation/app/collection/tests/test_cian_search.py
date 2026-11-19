"""Replay recorded Cian result shapes with real Chromium and no site traffic."""

import json
import time
from unittest import TestCase

from playwright.sync_api import Route

from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    SAMARA_REGION_ID,
)
from app.collection.browser import CollectionError, browser_page
from app.collection.browser import read_page as read_browser_page
from app.collection.collectors.cian_search import read_group, read_page, route_for
from app.collection.normalization import RejectedCard

HOST = "https://samara.cian.ru"
GROUP = (
    f"{HOST}/cat.php?deal_type=sale&engine_version=2&multi_id=330631068&offer_type=flat"
)


def script(state: dict[str, object]) -> str:
    entries = [{"key": "defaultState", "value": state}]
    return f"window._cianConfig = {{'frontend-serp': {json.dumps(entries)}}};"


class CianSearchTests(TestCase):
    def test_same_url_next_waits_for_a_new_ready_daily_page(self) -> None:
        # Arrange: replay the observed unpublished-to-ready transition on a real page.
        route = route_for(SAMARA_REGION_ID, "day_rent", HOUSE_TYPE_ID)
        assert route is not None
        query = {
            "_type": "suburbanrent",
            "engine_version": {"type": "term", "value": 2},
            "region": {"type": "terms", "value": [4608]},
            "for_day": {"type": "term", "value": "1"},
            "object_type": {"type": "terms", "value": [1]},
        }
        first = {
            "query": query,
            "list": {"page": 1, "status": "success", "hasMore": True, "current": 1},
            "offers": [{"id": 213894514}],
        }
        second = {
            "query": query,
            "list": {"page": 2, "status": "success", "hasMore": False, "current": 1},
            "offers": [{"id": 296497959}],
        }
        body = f"""<meta charset="utf-8">
            <a href="/rent/suburban/213894514/">Первый</a>
            <a href="https://cian.sutochno.ru/detail/1410601">Другой список</a>
            <button>Дальше</button>
            <script>{script(first)}
                document.querySelector("button").onclick = () => setTimeout(() => {{
                    document.querySelector("a").href = "/rent/suburban/296497959/";
                    document.querySelector("button").disabled = true;
                    {script(second)}
                }}, 250);
            </script>
        """

        def serve(request: Route) -> None:
            if request.request.url == route.start:
                request.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=body
                )
            else:
                request.abort()

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            page.goto(route.start)
            initial = read_page(page, route, time.monotonic() + 5)
            assert initial.next_control is not None
            following = read_browser_page(
                page,
                route.start,
                lambda: False,
                lambda _, deadline: read_page(
                    page, route, deadline, previous_page=initial.number
                ),
                accepts_redirect=route.permits_next,
                next_control=initial.next_control,
            )
            final_url = page.url

        # Assert
        self.assertEqual(final_url, route.start)
        self.assertEqual((initial.number, following.number), (1, 2))
        self.assertEqual(
            initial.urls, {"213894514": f"{HOST}/rent/suburban/213894514/"}
        )
        self.assertEqual(
            following.urls, {"296497959": f"{HOST}/rent/suburban/296497959/"}
        )
        self.assertIsNone(following.next_control)
        self.assertFalse(following.incomplete)

    def test_click_preserves_http_refusal(self) -> None:
        # Arrange
        next_url = f"{HOST}/next"
        visited: list[str] = []

        def respond(request: Route) -> None:
            url = request.request.url
            visited.append(url)
            body = f'<a href="{next_url}">Дальше</a>' if url == HOST + "/" else ""
            request.fulfill(status=403 if url == next_url else 200, body=body)

        with browser_page() as page:
            page.route("**/*", respond)
            page.goto(HOST)

            # Act / Assert
            with self.assertRaisesRegex(CollectionError, "^http_403$"):
                read_browser_page(
                    page,
                    HOST,
                    lambda: False,
                    lambda *_: None,
                    accepts_redirect=lambda url: url == next_url,
                    next_control="a",
                )
        self.assertEqual(visited, [HOST + "/", next_url])

    def test_group_uses_real_primary_cards_and_ignores_technical_second_page(
        self,
    ) -> None:
        # Arrange: use the recorded two-unit group, missing effective group region and
        # phantom pagination URL. Request identity must survive a redirect.
        route = route_for(SAMARA_REGION_ID, "sale", APARTMENT_TYPE_ID)
        assert route is not None
        query = {
            "_type": "flatsale",
            "engine_version": {"type": "term", "value": 2},
            "multi_id": {"type": "term", "value": 330631068},
        }
        state: dict[str, object] = {
            "results": {
                "jsonQuery": query,
                "offers": [{"id": 330631068}, {"id": 330631223}],
                "totalOffers": 2,
                "paginationUrls": [GROUP, f"{GROUP}&p=2"],
            }
        }
        body = f"""<meta charset="utf-8"><script>{script(state)}</script>
            <a href="/sale/flat/330631068/">Первая квартира</a>
            <a href="/sale/flat/330631223/">Вторая квартира</a>
            <a href="/sale/flat/330631159/">Рекомендация</a>
        """

        def serve(request: Route) -> None:
            if request.request.url == GROUP:
                request.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=body
                )
            else:
                request.abort()

        # Act
        with browser_page() as page:
            page.route("**/*", serve)
            page.goto(GROUP)
            result = read_group(page, route, time.monotonic() + 5, group_url=GROUP)
            with self.assertRaisesRegex(RejectedCard, "^listing_group_changed$"):
                read_group(
                    page,
                    route,
                    time.monotonic() + 5,
                    group_url=GROUP.replace("330631068", "330631223"),
                )

        # Assert
        self.assertEqual(set(result.urls), {"330631068", "330631223"})
        self.assertFalse(result.incomplete)
        self.assertIsNone(result.next_url)
        self.assertIsNone(result.next_control)
