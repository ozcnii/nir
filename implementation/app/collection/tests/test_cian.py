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

                # Act
                with self.assertRaisesRegex(RejectedCard, f"^{reason}$"):
                    parsed(url, offer, product)

                # Assert: the contradictory active snapshot never reaches storage.

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
            description=(
                "Кухня 9,2 м². Общая площадь квартиры 68,8 м². "
                "Продаётся комната 9,2 м² в соседней квартире."
            ),
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

    def test_explicit_foreign_id_prevents_an_idless_removal(self) -> None:
        # Arrange: the page also contains an offer belonging to another key.
        url, offer, _ = projection()
        requested = url.replace("330631159", "330631158")
        config = [script({"status": "deleted"}), script(offer)]

        # Act
        result = parse_card(requested, OBSERVED_AT, config, [], loaded_url=requested)

        # Assert
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["external_id"], "330631158")

    def test_supported_native_regional_redirect_keeps_the_key(self) -> None:
        # Arrange: regional host, tracking query and terminal slash each change.
        url, offer, product = projection()
        canonical = url.replace("samara.cian.ru", "www.cian.ru").rstrip("/")

        # Act
        result = parse_card(
            f"{url}?source=search#result",
            OBSERVED_AT,
            [script(offer)],
            [json.dumps(product)],
            loaded_url=canonical,
        )

        # Assert
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["external_id"], "330631159")
        assert result["state"] == "active"
        self.assertEqual(result["url"], canonical)

    def test_direct_room_evidence_after_a_preface_still_checks_scope(self) -> None:
        for description, reason in (
            ("Фото после ремонта. Продаётся комната 9,2 м².", "conflicting_room_area"),
            (
                "Ипотека возможна! Продаю долю в квартире, комната не выделена.",
                "unconfirmed_room_unit",
            ),
        ):
            with self.subTest(description=description):
                # Arrange
                url, offer, product = projection()
                offer.update(
                    category="roomSale",
                    roomArea="9.0",
                    roomsForSaleCount=1,
                    description=description,
                )

                # Act
                with self.assertRaisesRegex(RejectedCard, f"^{reason}$"):
                    parsed(url, offer, product)

                # Assert: the exact rejection reason is checked by assertRaisesRegex.

    def test_share_of_an_explicit_room_is_a_single_room_without_address_guessing(
        self,
    ) -> None:
        # Arrange: a direct offer identifies one room, but the source address
        # has no published room number. The description stays separate.
        url, offer, product = projection()
        offer.update(
            category="roomSale",
            roomArea="9.0",
            description="Продаю долю в квартире, выделена комната № 2.",
        )

        # Act
        snapshot = parsed(url, offer, product)

        # Assert
        self.assertEqual(snapshot["rooms"], 1)
        self.assertEqual(snapshot["area_m2"], Decimal("9.00"))
        self.assertNotIn("комната", snapshot["address"] or "")

    def test_house_offer_does_not_treat_a_group_or_unscoped_share_as_one_house(
        self,
    ) -> None:
        for description in (
            "Продаются два дома на одном участке.",
            "Продаётся дом 90,9 м² и второй дом 40 м² одним лотом.",
            "Предложение недели, продаётся дом и коттедж одним лотом.",
            "СРОЧНО. Продаётся 1/2 доля дома без отдельного входа.",
            "Продаётся проект коттеджа.",
        ):
            with self.subTest(description=description):
                # Arrange: replay the observed native house structure with a
                # controlled replacement of the seller's direct offer clause.
                url, offer, product = native_case("sale_house")
                offer["description"] = description

                # Act
                with self.assertRaisesRegex(RejectedCard, "^unconfirmed_house_unit$"):
                    parsed(url, offer, product)

                # Assert: no active whole-house snapshot is returned.

    def test_house_compares_only_area_of_the_offered_building(self) -> None:
        # Arrange: the known house has 90.9 m² and three bedrooms, not three rooms.
        url, offer, product = native_case("sale_house")
        offer["description"] = "Продаётся дом 90,9 м². Терраса 12 м², 3 спальни."

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertEqual(result["area_m2"], Decimal("90.90"))
        self.assertEqual(result["land_area_m2"], Decimal("2700.00"))
        self.assertIsNone(result["rooms"])

        # Arrange: the same offer now states a conflicting whole-house area.
        offer["description"] = "Продаётся дом общей площадью 85 м²."

        # Act
        with self.assertRaisesRegex(RejectedCard, "^conflicting_house_area$"):
            parsed(url, offer, product)

        # Assert: a difference between comparable whole-house areas rejects the input.

    def test_house_compares_area_of_the_plot_in_the_same_offer(self) -> None:
        for description, conflicted in (
            ("Продаётся дом 90,9 м² на участке 20 соток.", True),
            ("Продаётся дом на участке 2700 м².", False),
            ("Продаётся дом 90,9 м² на участке 27 соток.", False),
            ("Продаётся дом 90,9 м². Соседний дом на участке 20 соток.", False),
        ):
            with self.subTest(description=description):
                # Arrange: the retained projection gives 27 sotka for this offer.
                url, offer, product = native_case("sale_house")
                offer["description"] = description

                if conflicted:
                    # Act
                    with self.assertRaisesRegex(
                        RejectedCard, "^conflicting_land_area$"
                    ):
                        parsed(url, offer, product)
                    # Assert: comparable conflicting plot areas reject the card.
                else:
                    # Act
                    result = parsed(url, offer, product)

                    # Assert
                    self.assertEqual(result["land_area_m2"], Decimal("2700.00"))

    def test_native_daily_price_is_unavailable_if_terms_say_monthly(self) -> None:
        # Arrange: the observed daily label and matching JSON-LD remain unchanged.
        url, offer, product = native_case("day_flat")
        cast(dict[str, object], offer["bargainTerms"])["paymentPeriod"] = "monthly"

        # Act
        snapshot = parse_card(
            url,
            OBSERVED_AT,
            [script(offer, "от 3 000 ₽/сут.")],
            [json.dumps(product)],
            loaded_url=url,
        )

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["deal_type"], "day_rent")
        self.assertEqual(snapshot["price"], {"status": "unavailable"})

    def test_bundle_scope_does_not_use_singular_room_area(self) -> None:
        for published, description, expected_count, expected_area in (
            (None, "Продаются две комнаты.", 2, None),
            (2, None, 2, None),
            (
                2,
                "Продаются комнаты № 2 и № 3 общей площадью 18 м².",
                2,
                Decimal("18.00"),
            ),
            (
                None,
                "Продаются комната № 2 и комната № 3 одним лотом, общей площадью 18 м².",
                2,
                Decimal("18.00"),
            ),
            (
                None,
                "Продаются комната № 2, и комната № 3 общей площадью 18 м².",
                2,
                Decimal("18.00"),
            ),
            (
                None,
                "Продаются несколько комнат суммарной площадью 21 м².",
                None,
                Decimal("21.00"),
            ),
        ):
            with self.subTest(published=published, description=description):
                # Arrange: the singular roomArea field has no proven bundle scope.
                url, offer, product = projection()
                offer.update(
                    category="roomSale",
                    roomsForSaleCount=published,
                    roomArea="9.0",
                    description=description,
                )

                # Act
                result = parsed(url, offer, product)

                # Assert
                self.assertEqual(result["rooms"], expected_count)
                self.assertEqual(result["area_m2"], expected_area)

    def test_bundle_rejects_structural_count_conflict_but_ignores_optional_offer(
        self,
    ) -> None:
        # Arrange: one known room and a separately available neighbor.
        url, offer, product = projection()
        offer.update(
            category="roomSale",
            roomsForSaleCount=1,
            roomArea="9.0",
            description="Продаётся комната 9,0 м². Отдельно можно купить ещё комнату.",
        )

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertEqual((result["rooms"], result["area_m2"]), (1, Decimal("9.00")))

        for description, reason in (
            ("Продаются две комнаты одним лотом.", "conflicting_room_count"),
            (
                "Продаётся комната № 2 и комната № 3 одним лотом.",
                "conflicting_room_count",
            ),
            ("Хороший район, продаётся комната 9,2 м².", "conflicting_room_area"),
        ):
            with self.subTest(description=description):
                # Arrange: the structural room is singular and has area 9.0.
                offer["description"] = description

                # Act
                with self.assertRaisesRegex(RejectedCard, f"^{reason}$"):
                    parsed(url, offer, product)

                # Assert: the contradictory active snapshot never reaches storage.

    def test_block_checks_its_own_area_and_ignores_enclosing_house_total(self) -> None:
        # Arrange: one published block address and an enclosing building area.
        url, offer, product = native_case("sale_house")
        extend_native_address(offer, "дом 7", "блок 2")
        cast(dict[str, object], offer["building"])["totalArea"] = "170.0"
        offer["description"] = "Продаётся блок № 2 площадью 90,9 м²."

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertEqual(result["area_m2"], Decimal("90.90"))
        self.assertTrue(str(result["address"]).endswith("дом 7, блок 2"))

        # Arrange: conflict refers to the same block.
        offer["description"] = "Продаётся блок № 2 площадью 100 м²."

        # Act
        with self.assertRaisesRegex(RejectedCard, "^conflicting_house_area$"):
            parsed(url, offer, product)

        # Assert: the offered area is not replaced with another source.

    def test_block_scope_can_share_one_published_address_component(self) -> None:
        # Arrange: both roles are explicit even within one source component.
        url, offer, product = native_case("sale_house")
        extend_native_address(offer, "дом 7, блок 2")
        offer["description"] = "Продаётся только блок № 2 площадью 90,9 м²."

        # Act
        result = parsed(url, offer, product)

        # Assert
        self.assertTrue(str(result["address"]).endswith("дом 7, блок 2"))
        self.assertEqual(result["area_m2"], Decimal("90.90"))

    def test_incompatible_block_identity_is_rejected(self) -> None:
        for labels, description in (
            (("блок 2", "блок 3"), "Продаётся блок № 2."),
            (("блок 2",), "Продаётся блок № 3 площадью 90,9 м²."),
            (("блок 2",), "Продаётся весь дом."),
        ):
            with self.subTest(labels=labels, description=description):
                # Arrange
                url, offer, product = native_case("sale_house")
                extend_native_address(offer, "дом 7", *labels)
                offer["description"] = description

                # Act
                with self.assertRaisesRegex(RejectedCard, "^conflicting_house_unit$"):
                    parsed(url, offer, product)

                # Assert: the inconsistent scope never becomes a whole house.

    def test_marked_block_without_address_identity_does_not_become_house(self) -> None:
        for label in ("блок", "секция №", "дом 7 блок"):
            with self.subTest(label=label):
                # Arrange: the source denotes block scope without its identifier.
                url, offer, product = native_case("sale_house")
                extend_native_address(offer, label)
                offer["description"] = None

                # Act
                with self.assertRaisesRegex(RejectedCard, "^unconfirmed_house_unit$"):
                    parsed(url, offer, product)

                # Assert: a unit without a definite identifier is rejected.

    def test_native_subject_must_agree_with_observed_city_and_region_identity(
        self,
    ) -> None:
        for region_id, city, city_id in (
            (4609, "Самара", 4966),
            (4609, "Саратов", 4966),
            (4608, "Саратов", 4969),
        ):
            with self.subTest(region_id=region_id, city=city, city_id=city_id):
                # Arrange: all names and numeric codes come from recorded entities.
                url, offer, product = projection()
                cast(dict[str, object], offer["geo"])["address"] = [
                    {
                        "type": "location",
                        "id": region_id,
                        "locationTypeId": 2,
                        "fullName": "Саратовская область",
                    },
                    {
                        "type": "location",
                        "id": city_id,
                        "locationTypeId": 1,
                        "fullName": city,
                    },
                ]

                # Act
                with self.assertRaisesRegex(RejectedCard, "^conflicting_region$"):
                    parsed(url, offer, product)

                # Assert: the inconsistent pairing cannot enter the catalog.


def extend_native_address(offer: dict[str, object], *components: str) -> None:
    geo = cast(dict[str, object], offer["geo"])
    address = cast(list[dict[str, object]], geo["address"])
    address.extend({"type": "house", "fullName": name} for name in components)


def native_case(name: str) -> tuple[str, dict[str, object], dict[str, object]]:
    path = FIXTURE.with_name("cian-native-directions.json")
    cases = cast(list[dict[str, object]], json.loads(path.read_text())["cases"])
    case = next(item for item in cases if item["case"] == name)
    return (
        cast(str, case["url"]),
        cast(dict[str, object], case["offer"]),
        cast(dict[str, object], case["product"]),
    )
