import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from catalog.contracts import Snapshot
from collection.collectors.cian import parse_card, read_card
from collection.normalization import RejectedCard

FIXTURE = Path(__file__).with_name("fixtures") / "cian-330631159.json"
OBSERVED_AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


def projection() -> tuple[str, dict[str, object], dict[str, object]]:
    data = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    return (
        cast(str, data["url"]),
        cast(dict[str, object], data["offer"]),
        cast(dict[str, object], data["product"]),
    )


def script(offer: dict[str, object]) -> str:
    entry = {"key": "defaultState", "value": {"offerData": {"offer": offer}}}
    return (
        "window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        + json.dumps([entry])
        + ");"
    )


def parsed(url: str, offer: dict[str, object], product: dict[str, object]) -> Snapshot:
    return parse_card(url, OBSERVED_AT, [script(offer)], [json.dumps(product)])


class CianCardTests(TestCase):
    def test_saved_card_fields_from_page_scripts(self) -> None:
        # Arrange: reconstruct the inline envelope around the saved projection.
        url, offer, product = projection()
        html = (
            f"<script>{script(offer)}</script>"
            f'<script type="application/ld+json">{json.dumps(product)}</script>'
        )
        received: list[str] = []

        def serve_projection(route: Route) -> None:
            received.append(route.request.url)
            if route.request.url == url:
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.abort()

        # Act
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            context.route("**/*", serve_projection)
            page = context.new_page()
            page.goto(url)
            result = read_card(page, OBSERVED_AT)

        # Assert
        self.assertEqual(received, [url])
        self.assertEqual(
            result,
            {
                "source_code": "cian",
                "external_id": "330631159",
                "observed_at": OBSERVED_AT,
                "state": "active",
                "url": url,
                "region_id": 68,
                "property_type_id": 1,
                "deal_type": "sale",
                "market_segment": "new_build",
                "address": "Самарская область, Самара, район Советский",
                "latitude": Decimal("53.2100623"),
                "longitude": Decimal("50.2107520"),
                "area_m2": Decimal("47.21"),
                "rooms": 1,
                "floor": 2,
                "floors_total": 18,
                "land_area_m2": None,
                "description": None,
                "price": {
                    "status": "known",
                    "price_rub": Decimal("7246735.00"),
                    "price_kind": "listed",
                    "currency": "RUB",
                },
                "photos": {"status": "known", "urls": product["image"]},
            },
        )

    def test_rejects_wrong_identity_or_invalid_structural_number(self) -> None:
        for field, invalid in (
            ("id", 123456),
            ("category", None),
            ("totalArea", "47.211"),
            ("floorNumber", "2–14"),
        ):
            with self.subTest(field=field):
                # Arrange: synthetic damage to a saved projection.
                url, offer, product = projection()
                offer[field] = invalid

                with self.assertRaises(RejectedCard) as result:
                    # Act
                    parsed(url, offer, product)

                # Assert
                self.assertTrue(str(result.exception))

    def test_partial_price_and_photos_are_explicit(self) -> None:
        # Arrange: synthetic foreign currency and missing gallery.
        url, offer, product = projection()
        cast(dict[str, object], product["offers"])["priceCurrency"] = "USD"
        del product["image"]

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertEqual(result["state"], "active")
        assert result["state"] == "active"
        self.assertEqual(result["price"], {"status": "unavailable"})
        self.assertEqual(result["photos"], {"status": "unavailable"})

    def test_explicit_empty_gallery_is_known(self) -> None:
        # Arrange: synthetic empty primary gallery in both representations.
        url, offer, product = projection()
        offer["photos"] = []
        product["image"] = []

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertEqual(result["state"], "active")
        assert result["state"] == "active"
        self.assertEqual(result["photos"], {"status": "known", "urls": []})
