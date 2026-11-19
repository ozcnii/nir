"""Exercise the public JSON contract against the HTML catalog and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import DatabaseError, connection, models
from django.http import QueryDict
from django.test import Client, TransactionTestCase, override_settings
from playwright.sync_api import Page, sync_playwright

from app.catalog.catalog_filters import BIGINT_MAX, CatalogFilters
from app.catalog.catalog_queries import read_catalog
from app.catalog.models import Ad, PriceHistory, PriceKind, Property
from app.catalog.reference_defaults import SAMARA_REGION_ID
from app.catalog.tests.test_catalog_browser import OBSERVED
from app.catalog.tests.test_property_card import seed_property_card

API_LIST = "/api/properties/"
SEARCH = f"deal_type=sale&category=flat&region_id={SAMARA_REGION_ID}&area_min=69.50"
QUERY_MESSAGE = "Исправьте параметры запроса."


def seed_api_catalog() -> tuple[int, int]:
    property_id, removed_id = seed_property_card()
    Property.objects.filter(pk=property_id).update(
        latitude=Decimal("53.1"), longitude=Decimal("50.1234567")
    )
    Ad.objects.filter(property_id=property_id, external_id="listing-sale").update(
        observed_at=OBSERVED + timedelta(microseconds=123456)
    )
    return property_id, removed_id


def check_api_catalog(page: Page, origin: str, ids: tuple[int, int]) -> None:
    property_id, removed_id = ids
    last_api_page = f"{API_LIST}?{SEARCH}&page=2"

    # Arrange: keep public photos within the controlled browser fixture.
    page.route("https://media.example.test/**", lambda route: route.abort())

    # Act: follow the JSON link on each HTML catalog page.
    for number, size in ((1, 20), (2, 1)):
        page.goto(f"{origin}/catalog/?{SEARCH}&page={number}")
        json_link = page.get_by_role("link", name="JSON").get_attribute("href")
        assert json_link == f"{API_LIST}?{SEARCH}&page={number}"
        response = page.request.get(f"{origin}{json_link}")
        payload = response.json()

        # Assert: every ordered property and offer matches the actual HTML cards.
        assert response.status == 200
        assert response.headers["content-type"] == "application/json"
        cards = page.locator("article.result").all()
        assert len(cards) == len(payload["results"]) == size
        assert set(payload) == {
            "count",
            "page",
            "page_size",
            "next",
            "previous",
            "results",
        }
        assert payload["count"] == 21 and payload["page"] == number
        assert payload["page_size"] == 20
        assert payload["next"] == (last_api_page if number == 1 else None)
        assert payload["previous"] == (
            f"{API_LIST}?{SEARCH}&page=1" if number == 2 else None
        )
        for card, item in zip(cards, payload["results"], strict=True):
            link = card.get_by_role("link", name="Открыть объект").get_attribute("href")
            assert link is not None
            assert urlsplit(link).path == f"/properties/{item['id']}/"
            links = card.get_by_role("link", name="На площадку").all()
            assert [option.get_attribute("href") for option in links] == [
                offer["url"] for offer in item["ads"]
            ]
            assert all(offer["is_active"] is True for offer in item["ads"])
            assert all(
                "description" not in offer and "price_history" not in offer
                for offer in item["ads"]
            )

    # Act: follow the full card's JSON link without forwarding HTML search context.
    page.get_by_role("link", name="Открыть объект").click()
    assert parse_qs(urlsplit(page.url).query)["from"] == ["catalog"]
    detail_link = page.get_by_role("link", name="JSON").get_attribute("href")
    assert detail_link == f"{API_LIST}{property_id}/"
    detail_response = page.request.get(f"{origin}{detail_link}")
    detail = detail_response.json()
    removed_response = page.request.get(f"{origin}{API_LIST}{removed_id}/")

    # Assert: the detail keeps all offers, histories and fixed output types.
    assert detail_response.status == removed_response.status == 200
    assert detail["id"] == property_id
    assert "results" not in detail and "page" not in detail
    assert detail["region"] == {"id": SAMARA_REGION_ID, "name": "Самарская область"}
    assert (detail["latitude"], detail["longitude"], detail["area_m2"]) == (
        "53.1000000",
        "50.1234567",
        "70.00",
    )
    assert detail["land_area_m2"] is None
    offers = detail["ads"]
    assert [offer["external_id"] for offer in offers] == [
        "listing-day",
        "listing-month",
        "listing-sale",
        "listing-removed",
    ]
    day, month, sale, archived = offers
    assert day["photos"] == [] and day["photos_observed_at"] is None
    assert month["description"] is None and month["price_history"] == []
    assert (
        month["photos"] == [] and month["photos_observed_at"] == "2026-11-19T10:00:00Z"
    )
    assert month["latest_price"] is None
    assert sale["observed_at"] == "2026-11-19T10:00:00.123456Z"
    assert (
        sale["latest_price"]
        == sale["price_history"][-1]
        == {
            "price_rub": "10000000.00",
            "price_kind": "listed",
            "observed_at": "2026-11-18T10:00:00Z",
        }
    )
    assert [row["price_kind"] for row in sale["price_history"]] == ["from", "listed"]
    assert archived["is_active"] is False
    assert removed_response.json()["ads"][0]["is_active"] is False


# Keep Django's test media handler away from API paths.
@override_settings(MEDIA_URL="/media/")
class CatalogApiTests(StaticLiveServerTestCase):
    def test_api_matches_public_html_and_keeps_complete_detail(self) -> None:
        # Arrange
        ids = seed_api_catalog()
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            # Act / Assert
            check_api_catalog(context.new_page(), self.live_server_url, ids)

    def test_http_query_errors_share_validation_and_keep_all_fields(self) -> None:
        # Arrange
        invalid = (
            "page=1.5&rooms_min=1.5&area_min=.5&price_min=500&price_max=100"
            "&floor_min=0&floor_min=1&unexpected=1"
        )

        # Act
        with sync_playwright() as playwright:
            request = playwright.request.new_context(base_url=self.live_server_url)
            response = request.get(f"{API_LIST}?{invalid}")
            empty = request.get(API_LIST)
            missing_page = request.get(f"{API_LIST}?page=2")
            detail_query = request.get(f"{API_LIST}1/?from=outside&region_id=68")
            html = request.get(f"/catalog/?{invalid}")

            # Assert
            assert response.status == html.status == 400
            error = response.json()["error"]
            assert (
                error["code"] == "invalid_query" and error["message"] == QUERY_MESSAGE
            )
            assert {
                field: messages[0]["code"]
                for field, messages in error["fields"].items()
            } == {
                "page": "invalid_integer",
                "rooms_min": "invalid_integer",
                "area_min": "invalid_decimal",
                "price_min": "inverted_range",
                "price_max": "inverted_range",
                "floor_min": "repeated_parameter",
                "unexpected": "unknown_parameter",
            }
            assert detail_query.status == 400
            assert set(detail_query.json()["error"]["fields"]) == {
                "from",
                "region_id",
            }
            assert missing_page.status == 400
            assert missing_page.json()["error"]["fields"]["page"][0]["code"] == (
                "out_of_range"
            )
            assert empty.status == 200
            assert empty.json() == {
                "count": 0,
                "page": 1,
                "page_size": 20,
                "next": None,
                "previous": None,
                "results": [],
            }

    def test_debug_off_returns_json_errors_and_keeps_native_admin_css(self) -> None:
        # Arrange
        routes = (
            f"{API_LIST}1/",
            f"{API_LIST}0/",
            f"{API_LIST}invalid/",
            f"{API_LIST}{BIGINT_MAX + 1}/",
            f"{API_LIST}{'9' * 4500}/",
        )

        # Act
        with sync_playwright() as playwright:
            request = playwright.request.new_context(base_url=self.live_server_url)
            details = [request.get(route) for route in routes]
            unknown = request.get("/api/unknown")
            unsupported = request.post(API_LIST)
            css = request.get("/static/admin/css/base.css")
            with (
                self.assertLogs("django.request", level="ERROR") as logger,
                patch(
                    "app.catalog.json_api.read_catalog",
                    side_effect=DatabaseError("private database details"),
                ),
            ):
                failed = request.get(API_LIST)

            # Assert
            for response in (*details, unknown, unsupported, failed):
                assert response.headers["content-type"] == "application/json"
                assert set(response.json()) == {"error"}
                assert response.json()["error"]["fields"] == {}
            for response in details:
                assert response.status == 404
                assert response.json()["error"]["message"] == "Объект не найден."
            assert unknown.status == 404
            assert unknown.json()["error"]["message"] == "Ресурс не найден."
            assert unsupported.status == 405 and unsupported.headers["allow"] == "GET"
            assert unsupported.json()["error"]["code"] == "method_not_allowed"
            assert failed.status == 500 and failed.json()["error"]["code"] == (
                "internal_error"
            )
            assert "private database details" not in failed.text()
            assert "Traceback" not in failed.text()
            assert any(record.exc_info for record in logger.records)
            assert css.status == 200 and css.headers["content-type"].startswith(
                "text/css"
            )

        # Assert: bypassing CSRF for read-only JSON leaves the admin check in place.
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post("/admin/login/").status_code, 403)


class CatalogSnapshotTests(TransactionTestCase):
    def test_new_price_cannot_invalidate_the_returned_catalog_page(self) -> None:
        # Arrange: after selecting qualifying offers, publish a price above their filter.
        property_id, _ = seed_api_catalog()
        sale = Ad.objects.get(property_id=property_id, external_id="listing-sale")
        filters = CatalogFilters(QueryDict("price_max=10000000&page=2"))
        self.assertEqual(filters.field_errors(), {})
        get_history = PriceHistory.objects.filter

        def publish_new_price() -> None:
            try:
                PriceHistory.objects.create(
                    ad_id=sale.pk,
                    observed_at=OBSERVED + timedelta(seconds=1),
                    price_rub=Decimal("11000000"),
                    price_kind=PriceKind.LISTED,
                )
            finally:
                connection.close()

        def history_after_commit(**lookups: object) -> models.QuerySet[PriceHistory]:
            if "ad__in" in lookups:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(publish_new_price).result(timeout=5)
            return get_history(**lookups)

        # Act
        with patch.object(
            PriceHistory.objects, "filter", side_effect=history_after_commit
        ):
            page, results = read_catalog(filters)
        current_filters = CatalogFilters(QueryDict("price_max=10000000"))
        self.assertEqual(current_filters.field_errors(), {})
        current, _ = read_catalog(current_filters)

        # Assert: the first response stays coherent and the next sees the new price.
        self.assertEqual(page.paginator.count, 21)
        self.assertEqual([item.property.pk for item in results], [property_id])
        previous_price = results[0].offers[0].latest_price
        assert previous_price is not None
        self.assertEqual(previous_price.price_rub, Decimal("10000000"))
        self.assertEqual(current.paginator.count, 20)
