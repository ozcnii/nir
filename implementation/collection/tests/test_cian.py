import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from catalog.contracts import Snapshot
from collection.collectors.cian import card_key, parse_card, read_card
from collection.normalization import RejectedCard

FIXTURE = Path(__file__).with_name("fixtures") / "cian-330631159.json"
# Published offerPrice on the new-build card sampled on 2026-11-18.
NEW_BUILD_PRICE_LABEL = "7 246 735 ₽"
OBSERVED_AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


def projection() -> tuple[str, dict[str, object], dict[str, object]]:
    data = cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))
    return (
        cast(str, data["url"]),
        cast(dict[str, object], data["offer"]),
        cast(dict[str, object], data["product"]),
    )


def script(offer: dict[str, object], price_label: object = None) -> str:
    entry = {
        "key": "defaultState",
        "value": {"offerData": {"offer": offer, "offerPrice": price_label}},
    }
    return (
        "window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        + json.dumps([entry])
        + ");"
    )


def parsed(url: str, offer: dict[str, object], product: dict[str, object]) -> Snapshot:
    return parse_card(
        url,
        OBSERVED_AT,
        [script(offer, NEW_BUILD_PRICE_LABEL)],
        [json.dumps(product)],
        loaded_url=url,
    )


class CianCardTests(TestCase):
    def test_saved_card_fields_from_page_scripts(self) -> None:
        # Arrange: reconstruct the inline envelope around the saved projection.
        url, offer, product = projection()
        html = (
            f"<script>{script(offer, NEW_BUILD_PRICE_LABEL)}</script>"
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
            result = read_card(page, url, OBSERVED_AT)

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

    def test_rejects_invalid_structural_number_or_category(self) -> None:
        for field, invalid in (
            ("category", None),
            ("totalArea", "47.211"),
            ("floorNumber", "2–14"),
            ("roomsCount", 0),
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

    def test_invalid_seller_text_is_rejected_before_p3(self) -> None:
        # Arrange: the Stage 4 first-pass fixture injects a NUL into seller text.
        url, offer, product = projection()
        offer["description"] = "reject\x00this"

        with self.assertRaises(RejectedCard) as rejected:
            # Act
            parsed(url, offer, product)

        # Assert
        self.assertEqual(str(rejected.exception), "invalid_description")

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
                assert result["state"] == "active"
                self.assertEqual(result["photos"], expected)
                self.assertEqual(result["description"], offer["description"])

    def test_redirect_cannot_change_the_publication_key(self) -> None:
        # Arrange: the browser finishes on B after a request for A.
        url, offer, product = projection()
        requested = url.replace("330631159", "330631158")

        # Act
        result = parse_card(
            requested,
            OBSERVED_AT,
            [script(offer)],
            [json.dumps(product)],
            loaded_url=url,
        )

        # Assert
        self.assertEqual(
            result,
            {
                "source_code": "cian",
                "external_id": "330631158",
                "observed_at": OBSERVED_AT,
                "state": "unavailable",
            },
        )

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
                    for description in (
                        offer["description"],
                        "Уютная комната 9,2 м² с ремонтом — заезжай и живи!",
                        "Продаётся комната площадью 9,2 м².",
                        "Продаю комнату площадью 9,2 м².",
                        "Продаётся комната 9,2 кв. м.",
                    ):
                        with self.subTest(description=description):
                            # Arrange
                            case_offer = {**offer, "description": description}
                            with self.assertRaises(RejectedCard) as rejected:
                                # Act
                                parse_card(
                                    url,
                                    OBSERVED_AT,
                                    [script(case_offer, case["offerPrice"])],
                                    [json.dumps(case["product"])],
                                    loaded_url=url,
                                )

                            # Assert
                            self.assertEqual(
                                str(rejected.exception), "conflicting_room_area"
                            )
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

    def test_room_area_is_not_compared_to_the_kitchen_or_flat(self) -> None:
        # Arrange: the seller expressly states only a kitchen and a flat area.
        _, offer, product = projection()
        url = "https://samara.cian.ru/sale/flat/330631159/"
        offer.update(
            category="roomSale",
            roomArea="9.0",
            roomsForSaleCount=1,
            description=(
                "Площадь кухни 9,2 м². Общая площадь квартиры 68,8 м². "
                "Уютная комната 9,2 м² в соседней квартире."
            ),
        )

        # Act
        snapshot = parsed(url, offer, product)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["area_m2"], Decimal("9.00"))

    def test_monthly_room_compares_only_offered_area(self) -> None:
        # Arrange
        original, offer, product = projection()
        url = original.replace("/sale/", "/rent/")
        terms = cast(dict[str, object], offer["bargainTerms"])
        terms["paymentPeriod"] = "monthly"
        offer.update(
            category="roomRent",
            dealType="rent",
            roomArea="9.0",
            roomsForSaleCount=1,
            description="Сдаю комнату площадью 9,2 м².",
        )
        cast(dict[str, object], product["offers"])["url"] = url

        with self.assertRaises(RejectedCard) as rejected:
            # Act
            parsed(url, offer, product)

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_room_area")

    def test_missing_native_label_cannot_turn_from_into_listed(self) -> None:
        for deal, category, unit in (
            ("sale", "flatSale", "₽"),
            ("rent", "flatRent", "₽/мес."),
        ):
            with self.subTest(deal=deal):
                # Arrange: the amount, currency, Product type and priceType stay equal.
                original, offer, product = projection()
                url = original.replace("/sale/", f"/{deal}/")
                terms = cast(dict[str, object], offer["bargainTerms"])
                terms["priceType"] = "all"
                if deal == "rent":
                    terms["paymentPeriod"] = "monthly"
                offer.update(category=category, dealType=deal)
                cast(dict[str, object], product["offers"])["url"] = url
                result = []
                for price_label in (f"от 7 246 735 {unit}", None, f"7 246 735 {unit}"):
                    # Act
                    snapshot = parse_card(
                        url,
                        OBSERVED_AT,
                        [script(offer, price_label)],
                        [json.dumps(product)],
                        loaded_url=url,
                    )
                    assert snapshot["state"] == "active"
                    result.append(snapshot["price"])

                # Assert
                first, missing, last = result
                assert first["status"] == last["status"] == "known"
                self.assertEqual(
                    (first["price_rub"], first["price_kind"]),
                    (Decimal("7246735"), "from"),
                )
                self.assertEqual(missing, {"status": "unavailable"})
                self.assertEqual(
                    (last["price_rub"], last["price_kind"]),
                    (Decimal("7246735"), "listed"),
                )

    def test_unavailable_for_conflicting_embedded_identifier(self) -> None:
        for field in ("id", "cianId"):
            with self.subTest(field=field):
                # Arrange
                url, offer, product = projection()
                offer[field] = 999

                # Act
                snapshot = parsed(url, offer, product)

                # Assert
                self.assertEqual(snapshot["state"], "unavailable")
                self.assertEqual(snapshot["external_id"], "330631159")

    def test_same_id_with_redirect_to_another_card_path_is_unavailable(self) -> None:
        # Arrange
        url, offer, product = projection()
        rent = url.replace("/sale/", "/rent/")

        # Act
        snapshot = parse_card(
            url, OBSERVED_AT, [script(offer)], [json.dumps(product)], loaded_url=rent
        )

        # Assert
        self.assertEqual(snapshot["state"], "unavailable")

    def test_confirmed_regional_host_redirect_keeps_native_key(self) -> None:
        # Arrange
        url, offer, product = projection()
        loaded = url.replace("samara", "tolyatti").rstrip("/") + "?utm_campaign=check"

        # Act
        snapshot = parse_card(
            url, OBSERVED_AT, [script(offer)], [json.dumps(product)], loaded_url=loaded
        )

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["url"], loaded)
        self.assertEqual(snapshot["external_id"], "330631159")

    def test_only_recognized_urls_produce_card_keys(self) -> None:
        for url, key in (
            (
                "http://CIAN.SUTOCHNO.RU:80/detail/1906598/?guests=3#map",
                "/detail/1906598",
            ),
            ("https://www.cian.ru/sale/flat/1906598/", "1906598"),
            ("https://cian.sutochno.ru.example.org/detail/1906598", None),
            ("https://user@cian.sutochno.ru/detail/1906598", None),
            ("https://cian.sutochno.ru:8043/detail/1906598", None),
            ("https://cian.sutochno.ru/detail/01906598", None),
            ("https://cian.sutochno.ru/detail/%31906598", None),
            ("https://cian.sutochno.ru/detail/1906598/extra", None),
            ("https://new-city.cian.ru/sale/flat/1906598/", None),
            ("https://www.cian.ru/cat.php?multi_id=1906598", None),
        ):
            with self.subTest(url=url):
                # Arrange: known public formats and explicit K1 counterexamples.
                # Act
                result = card_key(url)

                # Assert
                self.assertEqual(result, key)

    def test_known_native_sale_with_unknown_land_unit_is_rejected(self) -> None:
        # Arrange: same house/card direction, only the structural unit was damaged.
        _, offer, product = projection()
        url = "https://samara.cian.ru/sale/suburban/330631159/"
        offer.update(
            category="houseSale",
            offerType="suburban",
            land={"area": "6.24", "areaUnitType": "acre"},
        )

        # Act
        with self.assertRaisesRegex(RejectedCard, "invalid_land_unit"):
            parsed(url, offer, product)
        # Assert: no P3 envelope can be submitted.

    def test_exact_land_conversion_address_and_coordinate_ties(self) -> None:
        # Arrange: synthetic formatting and ties on the same structural shape.
        _, offer, product = projection()
        url = "https://samara.cian.ru/sale/suburban/330631159/"
        geo = cast(dict[str, object], offer["geo"])
        address = cast(list[dict[str, object]], geo["address"])
        address.extend(
            [
                {"fullName": " ул.   Тестовая "},
                {"fullName": "7а/2"},
                {"fullName": "кв.  18"},
            ]
        )
        geo["coordinates"] = {"lat": "53.12345665", "lng": "50.12345675"}
        offer.update(
            category="houseSale",
            offerType="suburban",
            land={"area": "6,24", "areaUnitType": "sotka"},
        )

        # Act
        snapshot = parsed(url, offer, product)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["land_area_m2"], Decimal("624.00"))
        self.assertEqual(snapshot["latitude"], Decimal("53.1234566"))
        self.assertEqual(snapshot["longitude"], Decimal("50.1234568"))
        self.assertEqual(
            snapshot["address"],
            "Самарская область, Самара, район Советский, улица Тестовая, 7а/2, квартира 18",
        )


PARTNER_CASES = (
    (1906598, "Четырёхместный номер люкс", 142036, "Отель", 9000),
    (
        1157449,
        "Двухместный с 1 кроватью Зеркальный с джакузи и балконом",
        81597,
        "Апартаменты",
        5800,
    ),
)


def nuxt_script(root: dict[str, object]) -> str:
    # Rebuild the indexed envelope without carrying through the raw site payload.
    rows: list[object] = [None]

    def index(value: object) -> int:
        ref = len(rows)
        rows.append(None)
        if isinstance(value, dict):
            value = {key: index(v) for key, v in cast(dict[str, object], value).items()}
        elif isinstance(value, list):
            value = [index(v) for v in cast(list[object], value)]
        rows[ref] = value
        return ref

    rows[0] = ["ShallowReactive", index(root)]
    return json.dumps(rows)


def partner_card(
    identifier: int,
    *,
    name: str,
    hotel: dict[str, object] | None,
    amount: int,
    dates: Mapping[str, object] | None = None,
    label_unit: str = "за сутки",
) -> tuple[str, list[str], list[str]]:
    detail_key = f"detail:{identifier}"
    url = f"https://cian.sutochno.ru/{detail_key.replace(':', '/')}"
    detail = {
        "id": identifier,
        "status": {"name": "approved"},
        "url": f"https://sutochno.ru/front/searchapp/detail/{identifier}",
        "type": {"name": "room"},
        "title": "Комната",
        "name": name,
        "hotel": hotel,
        "location": {
            "city": {"title": "Самара"},
            "address": "ул. Тестовая, 7",
            "lat": "53.21",
            "lng": "50.12",
        },
        "currency": {"code": "RUB"},
        "media": {"images": [{"path": "//static.sutochno.ru/example.jpg?a=b"}]},
        "propertyGroups": [
            {
                "name": "basic_properties",
                "properties": [
                    {"name": "area", "title": "Площадь (м2)", "value": 13},
                    {"name": "rooms", "value": 3},
                ],
            },
            {
                "name": "detail_description",
                "properties": [
                    {"name": "description", "value": "Вид на отель.\nБез ремонта."}
                ],
            },
        ],
    }
    payload = {
        "data": {detail_key: detail},
        "vuex": {
            "detail": {
                "objectBasePrice": {
                    "object_id": identifier,
                    "base_price": {"price": amount, "currency_code": "RUB"},
                }
            },
            "search": {
                **({"checkIn": None, "checkOut": None} if dates is None else dates),
                "guests": {"adults": 2, "childrens": [], "pets": {"value": False}},
            },
        },
    }
    product = {
        "@type": "Product",
        "description": f"Цена {amount} RUB {label_unit}.",
        "image": "//static.sutochno.ru/example.jpg?a=b",
        "offers": {
            "@type": "AggregateOffer",
            "priceCurrency": "RUB",
            "lowPrice": amount,
        },
    }
    return url, [nuxt_script(payload)], [json.dumps(product)]


class CianPartnerTests(TestCase):
    def test_hotel_and_apart_hotel_are_excluded_before_storage(self) -> None:
        for identifier, name, hotel_id, hotel_type, amount in PARTNER_CASES:
            with self.subTest(identifier=identifier):
                # Arrange: reconstruct the identity and unit fields of observed cards.
                url, config, product = partner_card(
                    identifier,
                    name=name,
                    hotel={"id": hotel_id, "type": hotel_type},
                    amount=amount,
                )

                # Act
                with self.assertRaisesRegex(RejectedCard, "hotel_accommodation"):
                    parse_card(url, OBSERVED_AT, config, product, loaded_url=url)
                # Assert: no active/removed envelope reaches the catalog.

    def test_ordinary_partner_room_reaches_active_p3_even_with_hotel_in_description(
        self,
    ) -> None:
        # Arrange: synthetic supported unit using observed Nuxt field paths.
        url, config, product = partner_card(
            123, name="Комната рядом с парком", hotel=None, amount=5800
        )

        # Act
        snapshot = parse_card(
            url + "?utm_content=x", OBSERVED_AT, config, product, loaded_url=url + "/"
        )

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["source_code"], "cian")
        self.assertEqual(snapshot["external_id"], "/detail/123")
        self.assertEqual(snapshot["property_type_id"], 2)
        self.assertEqual(snapshot["deal_type"], "day_rent")
        self.assertIsNone(snapshot["rooms"])
        self.assertEqual(
            snapshot["address"], "Самарская область, Самара, улица Тестовая, 7"
        )
        self.assertEqual(snapshot["description"], "Вид на отель.\nБез ремонта.")
        self.assertEqual(
            snapshot["photos"],
            {"status": "known", "urls": ["https://static.sutochno.ru/example.jpg?a=b"]},
        )
        self.assertEqual(
            snapshot["price"],
            {
                "status": "known",
                "price_rub": Decimal("5800.00"),
                "price_kind": "listed",
                "currency": "RUB",
            },
        )

    def test_partner_tariff_requires_no_dates_and_an_explicit_daily_unit(self) -> None:
        for dates, unit in (
            ({"checkOut": None}, "за сутки"),
            ({"checkIn": "2026-11-23", "checkOut": "2026-11-25"}, "за сутки"),
            (None, "за проживание"),
        ):
            with self.subTest(dates=dates, unit=unit):
                # Arrange: same accepted fields with an unconfirmed base tariff.
                url, config, product = partner_card(
                    123,
                    name="Комната рядом с парком",
                    hotel=None,
                    amount=5800,
                    dates=dates,
                    label_unit=unit,
                )

                # Act
                snapshot = parse_card(url, OBSERVED_AT, config, product, loaded_url=url)

                # Assert
                assert snapshot["state"] == "active"
                self.assertEqual(snapshot["price"], {"status": "unavailable"})

    def test_partner_price_cannot_be_corroborated_for_a_foreign_publication(
        self,
    ) -> None:
        for change in (
            {"sku": 124},
            {"url": "https://cian.sutochno.ru/detail/124"},
            {"@id": "https://sutochno.ru/front/searchapp/detail/124#product"},
            {"mainEntityOfPage": {"@id": "https://cian.sutochno.ru/detail/124"}},
            {"offers": {"url": "https://cian.sutochno.ru/detail/124"}},
            {"image": "//static.sutochno.ru/foreign.jpg"},
        ):
            with self.subTest(change=change):
                # Arrange: same daily amount and otherwise unchanged valid Product.
                url, config, scripts = partner_card(
                    123, name="Комната рядом с парком", hotel=None, amount=5800
                )
                product = cast(dict[str, object], json.loads(scripts[0]))
                if "offers" in change:
                    cast(dict[str, object], product["offers"]).update(
                        cast(dict[str, object], change["offers"])
                    )
                else:
                    product.update(change)

                # Act
                snapshot = parse_card(
                    url, OBSERVED_AT, config, [json.dumps(product)], loaded_url=url
                )

                # Assert
                assert snapshot["state"] == "active"
                self.assertEqual(snapshot["price"], {"status": "unavailable"})

    def test_partner_state_for_a_different_id_is_unavailable(self) -> None:
        # Arrange
        url, config, product = partner_card(
            123, name="Комната рядом с парком", hotel=None, amount=5800
        )
        wrong = url.replace("123", "124")

        # Act
        snapshot = parse_card(wrong, OBSERVED_AT, config, product, loaded_url=wrong)

        # Assert
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertEqual(snapshot["external_id"], "/detail/124")
