"""Cian partner identity, exclusion and tariff checks."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest import TestCase

from app.collection.collectors.cian import card_key, parse_card
from app.collection.normalization import RejectedCard

OBSERVED_AT = datetime(2026, 11, 18, 12, tzinfo=UTC)

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
    description: str = "Вид на отель.\nБез ремонта.",
    city_id: int | None = 366625,
    city: str = "Самара",
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
            "city": {"id": city_id, "title": city},
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
                "properties": [{"name": "description", "value": description}],
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

    def test_partner_key_requires_exact_published_route(self) -> None:
        for suffix in ("/bad", "/../", "%32", "//"):
            # Arrange
            url = f"https://cian.sutochno.ru/detail/12{suffix}"

            # Act
            key = card_key(url)

            # Assert
            self.assertIsNone(key)

    def test_partner_rejects_conflicting_region_and_city_evidence(self) -> None:
        for city_id, city, breadcrumb in (
            (366625, "Самара", ("Саратовская область", "Самара")),
            (366625, "Саратов", ("Саратовская область", "Саратов")),
            (None, "Самара", ("Самарская область", "Саратов")),
        ):
            with self.subTest(city_id=city_id, city=city, breadcrumb=breadcrumb):
                # Arrange
                url, config, products = partner_card(
                    123,
                    name="Комната",
                    hotel=None,
                    amount=5800,
                    city_id=city_id,
                    city=city,
                )
                crumbs = {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"position": index, "name": name}
                        for index, name in enumerate(breadcrumb, 2)
                    ],
                }

                # Act
                with self.assertRaisesRegex(RejectedCard, "^conflicting_region$"):
                    parse_card(
                        url,
                        OBSERVED_AT,
                        config,
                        [*products, json.dumps(crumbs)],
                        loaded_url=url,
                    )

                # Assert: the conflicting address cannot produce an active P3.

    def test_partner_city_name_alone_does_not_supply_a_region(self) -> None:
        # Arrange: no published city ID and no region breadcrumb.
        url, config, products = partner_card(
            123, name="Комната", hotel=None, amount=5800, city_id=None
        )

        # Act
        with self.assertRaisesRegex(RejectedCard, "^invalid_region$"):
            parse_card(url, OBSERVED_AT, config, products, loaded_url=url)

        # Assert: do not infer the subject from an unverified city identity.
