"""Card projections and deliberately invalid inputs for the Domclick adapter."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from catalog.contracts import Snapshot
from collection.collectors.domclick import (
    DailyListingQuote,
    card_key,
    parse_card,
    read_card,
    read_daily_listing_quote,
)
from collection.normalization import RejectedCard

OBSERVED = datetime(2026, 11, 18, 13, tzinfo=UTC)
ROOM = "https://samara.domclick.ru/card/sale__room__2071752356"
HOUSE = "https://samara.domclick.ru/card/sale__house__2079647938"
PARTNER = "https://domclick.sutochno.ru/detail/2252883"
DAILY = "https://24.domclick.ru/card/1999c326-4f66-410d-8ea7-0d01a6e8af4e"
LISTING = "https://24.domclick.ru/samara"
DAILY_ASSET = "/homestay/60/a6/60a62c62f1ea470a894f49b660d12acc"
DAILY_PHOTO = f"https://img.dmclk.ru/s1200x800q80{DAILY_ASSET}.webp"


def native_proposal() -> dict[str, object]:
    # Selected paths from field-audit/rooms62 and the embedded card matrix.
    return {
        "id": 2071752356,
        "offerType": "room",
        "dealType": "sale",
        "status": 2,
        "roomsOffered": 1,
        "address": {
            "displayName": "Россия, Самара, Солнечная улица, 7",
            "parents": [
                {"kind": "locality", "name": "Самара"},
                {"kind": "province", "subkind": "region", "name": "Самарская область"},
                {"kind": "province", "name": "Приволжский федеральный округ"},
            ],
            "position": {"lat": 53.240691, "lon": 50.190295},
        },
        "house": {"info": {"floors": 9}},
        "objectInfo": {
            "area": 68.8,
            "roomArea": 9.2,
            "rooms": 3,
            "floor": 1,
            "description": "Текст продавца: undefined — сохранить.\nРазмер 9,2 м².",
        },
        "priceInfo": {"currency": "RUB", "price": 1400000},
    }


def partner_projection() -> dict[str, object]:
    # Selected public paths from the live 2252883 card.
    return {
        "id": 2252883,
        "url": "https://sutochno.ru/front/searchapp/detail/2252883",
        "status": {"name": "approved"},
        "type": {"name": "apartment"},
        "title": "1-комнатная квартира",
        "hotel": None,
        "location": {
            "city": {"id": 366640, "title": "Новокуйбышевск"},
            "address": "Суворова улица, 13А",
            "lat": 53.108529999998,
            "lng": 49.949734,
        },
        "media": {
            "images": [
                {
                    "path": "//static.sutochno.ru/doc/files/objects/2/252/883/699d88d60d929.jpg"
                }
            ]
        },
        "propertyGroups": [
            {
                "name": "basic_properties",
                "properties": [
                    {"name": "rooms", "value": 1},
                    {"name": "area", "title": "Площадь (м2)", "value": 23},
                    {"name": "floor", "value": 4},
                    {"name": "max_floor", "value": 5},
                ],
            },
            {
                "name": "detail_description",
                "properties": [
                    {"name": "description", "value": "Описание продавца.\n23 м²."},
                ],
            },
        ],
    }


def nuxt_script(detail: dict[str, object]) -> str:
    entries: list[object] = [["ShallowReactive", 1]]

    def encode(value: object) -> int:
        index = len(entries)
        entries.append(None)
        if isinstance(value, dict):
            entries[index] = {key: encode(member) for key, member in value.items()}
        elif isinstance(value, list):
            entries[index] = [encode(member) for member in value]
        else:
            entries[index] = value
        return index

    encode({"data": {"detail:2252883": detail}})
    return json.dumps(entries)


def daily_projection() -> dict[str, object]:
    # Selected public paths from field-audit/domclick64-card-fields.json and live DOM.
    return {
        "uid": "0f993e54-dbfc-4633-bf0a-4224738136b3",
        "status": "published",
        "category": "flat",
        "geoInfo": {
            "address": "Самарская область, пос. Придорожный, Николаевский проспект, 31",
            "lat": 53.112009,
            "lon": 50.145113,
        },
        "area": 36,
        "areaUnit": "м²",
        "rooms": 1,
        "floor": 6,
        "floorsMax": 9,
        "photos": [{"url": f"{DAILY_ASSET}.jpg"}],
        "basePrice": 0,
    }


def ssr(source: dict[str, object]) -> str:
    return "window.__SSR_STATE__ = " + json.dumps(source) + ";"


def daily_ssr(data: dict[str, object]) -> str:
    return ssr(
        {
            "inventory": {
                "searchData": {"id": DAILY.rsplit("/", 1)[1]},
                "fetchStatus": "success",
                "data": data,
            }
        }
    )


def read_native(
    url: str, data: dict[str, object], *, removed: bool = False
) -> Snapshot:
    return parse_card(
        url,
        OBSERVED,
        [ssr({"productCard": data})],
        [],
        loaded_url=url,
        removed_marker=removed,
    )


class DomclickTests(TestCase):
    def test_native_room_keeps_the_offered_room_and_seller_text(self) -> None:
        # Arrange
        data = native_proposal()
        script = ssr({"productCard": data, "other": None}).replace(
            '"other": null', '"other": undefined'
        )

        # Act
        result = parse_card(ROOM, OBSERVED, [script], [], loaded_url=ROOM)

        # Assert
        self.assertEqual(
            result,
            {
                "source_code": "domclick",
                "external_id": "/card/sale__room__2071752356",
                "observed_at": OBSERVED,
                "state": "active",
                "url": ROOM,
                "region_id": 68,
                "property_type_id": 2,
                "deal_type": "sale",
                "market_segment": None,
                "address": "Самарская область, Самара, Солнечная улица, 7",
                "latitude": Decimal("53.2406910"),
                "longitude": Decimal("50.1902950"),
                "area_m2": Decimal("9.20"),
                "rooms": 1,
                "floor": 1,
                "floors_total": 9,
                "land_area_m2": None,
                "description": "Текст продавца: undefined — сохранить.\nРазмер 9,2 м².",
                "price": {
                    "status": "known",
                    "price_rub": Decimal("1400000.00"),
                    "price_kind": "listed",
                    "currency": "RUB",
                },
                "photos": {"status": "unavailable"},
            },
        )

    def test_home_converts_land_exactly_and_keeps_partial_address(self) -> None:
        # Arrange
        data = native_proposal()
        data.update(
            {
                "id": 2079647938,
                "offerType": "house",
                "address": {
                    "displayName": "Россия, Самара, Северный переулок",
                    "parents": [
                        {"kind": "locality", "name": "СНТ Яблонька"},
                        {"kind": "locality", "name": "Самара"},
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {"area": 100, "rooms": None, "floor": None},
                "house": {"info": {"floors": 1}},
                "land": {"area": "6,24", "areaUnit": "сотка"},
            }
        )

        # Act
        result = read_native(HOUSE, data)

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["area_m2"], Decimal("100.00"))
        self.assertEqual(result["land_area_m2"], Decimal("624.00"))
        self.assertIsNone(result["floor"])
        self.assertEqual(
            result["address"],
            "Самарская область, Самара, СНТ Яблонька, Северный переулок",
        )

    def test_wrong_unit_and_nonrepresentable_area_stop_before_p3(self) -> None:
        for land in (
            {"area": 4, "areaUnit": "акр"},
            {"area": "1.00001", "areaUnit": "сотка"},
        ):
            with self.subTest(land=land):
                # Arrange
                data = native_proposal()
                data.update({"id": 2079647938, "offerType": "house", "land": land})

                # Act
                with self.assertRaises(RejectedCard) as rejected:
                    read_native(HOUSE, data)

                # Assert
                self.assertIn(
                    str(rejected.exception), ("invalid_land_unit", "invalid_precision")
                )

    def test_valid_key_has_exact_host_and_path(self) -> None:
        # Arrange
        positive = {
            "https://DOMCLICK.SUTOCHNO.RU:443/detail/2252883/?guests=3#photo": "/detail/2252883",
            DAILY.upper()
            .replace("HTTPS", "https")
            .replace("/CARD/", "/card/"): card_key(DAILY),
            ROOM + "/?foo=1": "/card/sale__room__2071752356",
        }
        negative = (
            "https://domclick.sutochno.ru.example.org/detail/2252883",
            "https://domclick.sutochno.ru/detail/02252883",
            "https://domclick.sutochno.ru:444/detail/2252883",
            "https://user@domclick.sutochno.ru/detail/2252883",
            "https://domclick.sutochno.ru/detail/%32252883",
            "https://domclick.sutochno.ru/detail/2252883/other",
            "https://unknown.domclick.ru/card/sale__room__2071752356",
            "https://24.domclick.ru/card/sale__room__2071752356",
        )

        # Act
        supported = {candidate: card_key(candidate) for candidate in positive}
        refused = [card_key(candidate) for candidate in negative]

        # Assert
        self.assertEqual(supported, positive)
        self.assertEqual(refused, [None] * len(negative))

    def test_foreign_publication_does_not_replace_a_known_key(self) -> None:
        # Arrange
        data = native_proposal()
        data["id"] = 2071752357
        script = ssr({"productCard": data})

        # Act
        mismatch = parse_card(ROOM, OBSERVED, [script], [], loaded_url=ROOM)
        redirect = parse_card(ROOM, OBSERVED, [script], [], loaded_url=DAILY)

        # Assert
        expected = {
            "source_code": "domclick",
            "external_id": "/card/sale__room__2071752356",
            "observed_at": OBSERVED,
            "state": "unavailable",
        }
        self.assertEqual(mismatch, expected)
        self.assertEqual(redirect, expected)

    def test_removed_requires_explicit_marker_and_rejects_contradictory_identity(
        self,
    ) -> None:
        # Arrange
        data = native_proposal()
        data.update({"id": 2079647938, "offerType": "house", "status": 8})

        # Act
        bare = read_native(HOUSE, data)
        removed = read_native(HOUSE, data, removed=True)
        conflict = read_native(ROOM, data, removed=True)

        # Assert
        self.assertEqual(bare["state"], "unavailable")
        self.assertEqual(removed["state"], "removed")
        self.assertEqual(conflict["state"], "unavailable")

    def test_partner_decodes_refs_and_returns_the_confirmed_flat(self) -> None:
        # Arrange
        script = nuxt_script(partner_projection())

        # Act
        result = parse_card(
            PARTNER, OBSERVED, [], [script], loaded_url=PARTNER + "?price_per=1"
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["external_id"], "/detail/2252883")
        self.assertEqual(result["source_code"], "domclick")
        self.assertEqual(result["deal_type"], "day_rent")
        self.assertEqual(result["latitude"], Decimal("53.1085300"))
        self.assertEqual(result["price"], {"status": "unavailable"})
        self.assertEqual(result["description"], "Описание продавца.\n23 м².")
        self.assertEqual(result["photos"]["status"], "known")

    def test_partner_room_with_hotel_parent_never_makes_p3(self) -> None:
        # Arrange: deliberate unit counterexample using the observed Nuxt shape.
        detail = partner_projection()
        detail.update({"type": {"name": "room"}, "hotel": {"id": 1, "title": "Отель"}})
        script = nuxt_script(detail)

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            parse_card(PARTNER, OBSERVED, [], [script], loaded_url=PARTNER)

        # Assert
        self.assertEqual(str(rejected.exception), "hotel_or_ambiguous_room")

    def test_daily_read_uses_same_link_visible_one_night_quote_and_main_gallery(
        self,
    ) -> None:
        # Arrange: replay selected live DOM controls and publication-shaped state.
        dom = (
            '<div data-e2e-id="search-form-datepicker"><input value=""></div>'
            '<div data-e2e-id="search-form-guests-control"><input value="1 гость"></div>'
            '<div data-e2e-id="snippet-item">'
            f'<a href="{DAILY}">Card</a>'
            '<span data-e2e-id="short-rent-price">3 910 ₽</span>'
            '<span data-e2e-id="short-rent-price-per-night">3 910 ₽ за сутки</span>'
            "</div>"
        )
        card = (
            f"<script>{daily_ssr(daily_projection())}delete window.__SSR_STATE__;</script>"
            '<div data-e2e-id="gallery-widget">'
            f'<img src="{DAILY_PHOTO}"></div>'
        )

        def serve(route: Route) -> None:
            if route.request.url in (LISTING, DAILY):
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=dom if route.request.url == LISTING else card,
                )
            else:
                route.abort()

        # Act
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium", headless=True) as browser,
            browser.new_context() as context,
        ):
            context.route("**/*", serve)
            listing = context.new_page()
            listing.goto(LISTING)
            quote = read_daily_listing_quote(listing, DAILY)
            page = context.new_page()
            page.goto(DAILY)
            snapshot = read_card(page, DAILY, OBSERVED, quote=quote)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["external_id"], card_key(DAILY))
        self.assertEqual(snapshot["area_m2"], Decimal("36.00"))
        self.assertEqual(
            snapshot["price"],
            {
                "status": "known",
                "price_rub": Decimal("3910.00"),
                "price_kind": "listed",
                "currency": "RUB",
            },
        )
        self.assertEqual(snapshot["photos"], {"status": "known", "urls": [DAILY_PHOTO]})

    def test_daily_wrong_context_has_no_price_and_incomplete_photos_stay_unavailable(
        self,
    ) -> None:
        for quote in (
            None,
            DailyListingQuote(
                DAILY, "1–2 мая", "1 гость", "3 910 ₽", "3 910 ₽ за сутки"
            ),
            DailyListingQuote(DAILY, "", "2 гостя", "3 910 ₽", "3 910 ₽ за сутки"),
            DailyListingQuote(DAILY, "", "1 гость", "3 910 ₽", "3 910 ₽ за неделю"),
            DailyListingQuote(DAILY, "", "1 гость", "3 910 ₽", "2 000 ₽ за сутки"),
        ):
            with self.subTest(quote=quote):
                # Arrange
                script = daily_ssr(daily_projection())

                # Act
                result = parse_card(
                    DAILY,
                    OBSERVED,
                    [script],
                    [],
                    loaded_url=DAILY + "?adt=1",
                    quote=quote,
                )

                # Assert
                assert result["state"] == "active"
                self.assertEqual(result["price"], {"status": "unavailable"})
                self.assertEqual(result["photos"], {"status": "unavailable"})

    def test_daily_public_id_is_never_taken_from_internal_uid(self) -> None:
        # Arrange
        data = daily_projection()
        card_uuid = DAILY.rsplit("/", 1)[1]
        data["uid"] = card_uuid
        script = daily_ssr(data).replace(
            f'"searchData": {{"id": "{card_uuid}"}}',
            '"searchData": {"id": "98497d9e-fe9c-44b1-8ec5-902e6d27763b"}',
        )

        # Act
        result = parse_card(DAILY, OBSERVED, [script], [], loaded_url=DAILY)

        # Assert
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["external_id"], card_key(DAILY))

    def test_daily_keeps_from_price_and_half_even_coordinate_rounding(self) -> None:
        # Arrange
        data = daily_projection()
        data["geoInfo"] = {
            "address": "Самарская область, пос. Придорожный, Николаевский проспект, 31",
            "lat": 53.11200925,
            "lon": 50.14511335,
        }
        quote = DailyListingQuote(
            DAILY, "", "1 гость", "от 2 300 ₽", "от 2 300 ₽ за сутки"
        )

        # Act
        result = parse_card(
            DAILY, OBSERVED, [daily_ssr(data)], [], loaded_url=DAILY, quote=quote
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["latitude"], Decimal("53.1120092"))
        self.assertEqual(result["longitude"], Decimal("50.1451134"))
        self.assertEqual(
            result["price"],
            {
                "status": "known",
                "price_rub": Decimal("2300.00"),
                "price_kind": "from",
                "currency": "RUB",
            },
        )
