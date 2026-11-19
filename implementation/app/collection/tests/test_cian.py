import json
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from typing import cast
from unittest import TestCase
from unittest.mock import Mock, patch

from playwright.sync_api import Page

from app.catalog.contracts import ActiveSnapshot
from app.collection.collectors.cian import card_key, parse_card
from app.collection.normalization import RejectedCard
from app.collection.one_card import CollectionStopped, _read_card
from app.collection.tests.cian_fixture import projection, script

OBSERVED_AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


def parsed(
    url: str, offer: dict[str, object], product: dict[str, object]
) -> ActiveSnapshot:
    key = card_key(url)
    assert key is not None
    return parse_card(
        key, OBSERVED_AT, [script(offer)], [json.dumps(product)], loaded_url=url
    )


class CianCardTests(TestCase):
    def test_saved_card_fields_from_page_scripts(self) -> None:
        # Arrange
        url, offer, product = projection()

        # Act
        result = parsed(url, offer, product)

        # Assert
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
        for field, invalid, reason in (
            ("id", 123456, "invalid_card_direction"),
            ("category", None, "invalid_card_direction"),
            ("totalArea", "47.211", "invalid_precision"),
            ("floorNumber", "2–14", "invalid_number"),
            ("roomsCount", 0, "unconfirmed_studio"),
        ):
            with self.subTest(field=field):
                # Arrange: synthetic damage to a saved projection.
                url, offer, product = projection()
                offer[field] = invalid

                # Act / Assert
                with self.assertRaisesRegex(RejectedCard, f"^{reason}$"):
                    parsed(url, offer, product)

    def test_extra_invalid_json_ld_does_not_discard_valid_product(self) -> None:
        # Arrange
        url, offer, product = projection()
        invalid_product = '{"score": 1e10000000000000000000}'

        # Act
        parsed_with_invalid = parse_card(
            "330631159",
            OBSERVED_AT,
            [script(offer)],
            [invalid_product, json.dumps(product)],
            loaded_url=url,
        )

        # Assert
        self.assertEqual(parsed_with_invalid, parsed(url, offer, product))

    def test_partial_price_and_photos_are_explicit(self) -> None:
        # Arrange: synthetic foreign currency and a malformed gallery URL.
        url, offer, product = projection()
        cast(dict[str, object], product["offers"])["priceCurrency"] = "USD"
        product["image"] = [
            "https://bad_host.example/x",
            *cast(list[str], product["image"])[1:],
        ]

        # Act
        with self.assertLogs(level="WARNING") as logs:
            result = parsed(url, offer, product)

        # Assert
        self.assertIn(
            "cian/330631159 price unavailable: price_not_rub", "\n".join(logs.output)
        )
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["price"], {"status": "unavailable"})
        self.assertEqual(result["photos"], {"status": "unavailable"})

    def test_explicit_empty_gallery_is_known(self) -> None:
        for primary, expected in (
            (None, {"status": "unavailable"}),
            ([], {"status": "known", "urls": []}),
        ):
            with self.subTest(primary=primary):
                # Arrange: synthetic incomplete or confirmed empty gallery.
                url, offer, product = projection()
                if primary is None:
                    del offer["photos"]
                else:
                    offer["photos"] = primary
                product["image"] = []
                offer["description"] = "Тест: ремонт не требуется.   Площадь 47,21 м²."

                # Act
                result = parsed(url, offer, product)

                # Assert
                self.assertEqual(result["photos"], expected)
                self.assertEqual(result["description"], offer["description"])

    def test_redirect_cannot_change_the_publication_key(self) -> None:
        # Arrange: the browser finishes on a different offer.
        url, offer, product = projection()

        # Act / Assert
        with self.assertRaisesRegex(RejectedCard, "^unexpected_redirect$"):
            parse_card(
                "330631158",
                OBSERVED_AT,
                [script(offer)],
                [json.dumps(product)],
                loaded_url=url,
            )

    def test_stop_requested_during_capture_takes_precedence_over_rejection(
        self,
    ) -> None:
        # Arrange
        url, offer, _ = projection()
        page = Mock(spec=Page)
        page.goto.return_value.ok = True
        page.goto.return_value.status = 200
        stopped = Event()

        def rejected(*args: object) -> None:
            stopped.set()
            raise RejectedCard("source_captcha")

        # Act / Assert
        with (
            patch("app.collection.one_card.capture_card", side_effect=rejected),
            self.assertRaises(CollectionStopped),
        ):
            _read_card(cast(Page, page), url, str(offer["id"]), stopped.is_set)
