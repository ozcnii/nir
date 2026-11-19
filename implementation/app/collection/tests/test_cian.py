import json
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from typing import cast
from unittest import TestCase
from unittest.mock import Mock, patch

from playwright.sync_api import Page

from app.catalog.contracts import ActiveSnapshot
from app.collection.collectors.cian import parse_card
from app.collection.collectors.cian_fields import RejectedCard
from app.collection.one_card import CollectionStopped, _read_card
from app.collection.tests.cian_fixture import FIXTURE, projection, script

OBSERVED_AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


def parsed(
    url: str, offer: dict[str, object], product: dict[str, object]
) -> ActiveSnapshot:
    result = parse_card(
        url, OBSERVED_AT, [script(offer)], [json.dumps(product)], loaded_url=url
    )
    assert result["state"] == "active"
    return result


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
            url,
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
        result = parsed(url, offer, product)

        # Assert
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

        # Act
        snapshot = parse_card(
            url.replace("330631159", "330631158"),
            OBSERVED_AT,
            [script(offer)],
            [json.dumps(product)],
            loaded_url=url,
        )

        # Assert
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertEqual(snapshot["external_id"], "330631158")

    def test_stop_requested_during_capture_takes_precedence_over_rejection(
        self,
    ) -> None:
        # Arrange
        url, _, _ = projection()
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
            _read_card(cast(Page, page), url, stopped.is_set)

    def test_replay_published_native_directions(self) -> None:
        expectations = {
            "resale_flat": (1, "sale", "resale", "110.00", 3, None, "listed"),
            "sale_house": (3, "sale", None, "90.90", None, "2700.00", "listed"),
            "month_flat": (1, "month_rent", None, "40.00", 1, None, "listed"),
            "month_room": (2, "month_rent", None, "14.00", 1, None, "listed"),
            "month_house": (3, "month_rent", None, "270.00", None, "2000.00", None),
            "day_flat": (1, "day_rent", None, "32.00", 1, None, "from"),
            "day_room": (2, "day_rent", None, "60.00", 1, None, "from"),
            "day_house": (3, "day_rent", None, "250.00", None, "1000.00", "from"),
        }
        path = FIXTURE.with_name("cian-native-directions.json")
        cases = cast(list[dict[str, object]], json.loads(path.read_text())["cases"])
        for case in cases:
            with self.subTest(case=case["case"]):
                # Arrange: rebuild the source envelope around a saved field projection.
                name = cast(str, case["case"])
                url = cast(str, case["url"])
                offer = cast(dict[str, object], case["offer"])

                if name == "sale_room":
                    # 319653899 publishes 9.0 in roomArea. Each form
                    # explicitly describes the offered room as 9.2 m².
                    with self.assertRaisesRegex(
                        RejectedCard, "^conflicting_room_area$"
                    ):
                        # Act
                        parse_card(
                            url,
                            OBSERVED_AT,
                            [script(offer, case["offerPrice"])],
                            [json.dumps(case["product"])],
                            loaded_url=url,
                        )

                    # Assert: the conflicting seller measurement rejects the snapshot.
                    continue

                # Act
                snapshot = parse_card(
                    f"{url}?source=search#result",
                    OBSERVED_AT,
                    [script(offer, case["offerPrice"])],
                    [json.dumps(case["product"])],
                    loaded_url=url,
                )

                # Assert
                self.assertEqual(snapshot["external_id"], str(offer["id"]))
                if name == "removed":
                    self.assertEqual(snapshot["state"], "removed")
                    assert snapshot["state"] == "removed"
                    self.assertEqual(snapshot["deal_type"], "sale")
                    continue
                assert snapshot["state"] == "active"
                property_id, deal, market, area, rooms, land, kind = expectations[name]
                self.assertEqual(snapshot["property_type_id"], property_id)
                self.assertEqual(snapshot["deal_type"], deal)
                self.assertEqual(snapshot["market_segment"], market)
                self.assertEqual(snapshot["area_m2"], Decimal(area))
                self.assertEqual(snapshot["rooms"], rooms)
                self.assertEqual(
                    snapshot["land_area_m2"], Decimal(land) if land else None
                )
                if property_id == 3:
                    self.assertIsNone(snapshot["floor"])
                if kind:
                    actual_price = snapshot["price"]
                    assert actual_price["status"] == "known"
                    self.assertEqual(actual_price["price_kind"], kind)
                    expected_price = cast(
                        dict[str, int],
                        cast(dict[str, object], case["product"])["offers"],
                    )["price"]
                    self.assertEqual(actual_price["price_rub"], Decimal(expected_price))
                else:
                    self.assertEqual(snapshot["price"], {"status": "unavailable"})

    def test_native_region_comes_from_structured_subject(self) -> None:
        # Arrange: the region ID is source-specific. P3 uses the catalog reference.
        url, offer, product = projection()
        url = url.replace("samara.cian.ru", "saratov.cian.ru")
        cast(dict[str, object], product["offers"])["url"] = url
        cast(dict[str, object], offer["geo"])["address"] = [
            {"type": "location", "locationTypeId": 2, "fullName": "Саратовская область"}
        ]

        # Act
        snapshot = parsed(url, offer, product)

        # Assert
        self.assertEqual(snapshot["region_id"], 69)
        self.assertEqual(snapshot["address"], "Саратовская область")

    def test_native_identity_must_agree_across_url_and_state(self) -> None:
        for field, value in (
            ("id", 999),
            ("cianId", 999),
            ("dealType", "rent"),
            ("offerType", "suburban"),
        ):
            with self.subTest(field=field):
                # Arrange
                url, offer, product = projection()
                offer[field] = value

                # Act
                snapshot = parse_card(
                    url,
                    OBSERVED_AT,
                    [script(offer)],
                    [json.dumps(product)],
                    loaded_url=url,
                )

                # Assert
                self.assertEqual(snapshot["state"], "unavailable")
                self.assertEqual(snapshot["external_id"], "330631159")

    def test_native_room_keeps_structured_area_and_offer_count(self) -> None:
        # Arrange: the unrelated kitchen number must not replace or invalidate
        # the published room area. The flat has three rooms, one is offered.
        url, offer, product = projection()
        offer.update(
            category="roomSale",
            roomsCount=3,
            roomsForSaleCount=1,
            roomArea="9.0",
            description="Кухня 9,2 м². Общая площадь квартиры 68,8 м².",
        )

        # Act
        snapshot = parsed(url, offer, product)

        # Assert
        self.assertEqual(snapshot["rooms"], 1)
        self.assertEqual(snapshot["area_m2"], Decimal("9.00"))

    def test_explicit_removed_sale_can_lose_active_fields(self) -> None:
        # Arrange: deleted state has no published active ID.
        url, _, _ = projection()

        # Act
        result = parse_card(
            url, OBSERVED_AT, [script({"status": "deleted"})], [], loaded_url=url
        )

        # Assert
        self.assertEqual(result["state"], "removed")
        self.assertEqual(result["external_id"], "330631159")
