"""Card projections and deliberately invalid inputs for the Domclick adapter."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
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
    def test_native_room_facts_keep_offered_count_and_reject_conflict(self) -> None:
        # Arrange
        data = native_proposal()
        info = cast(dict[str, object], data["objectInfo"])
        info["description"] = "Площадь кухни 10 м²."
        info["flatNumber"] = "25"
        address = cast(dict[str, object], data["address"])
        address["displayName"] = (
            "Россия, Самара, Солнечная улица, 7, квартира 25, комната 2"
        )

        # Act
        single = read_native(ROOM, data)
        unknown = read_native(ROOM, {**data, "roomsOffered": None})
        multiple = read_native(ROOM, {**data, "roomsOffered": 2})

        # Assert
        assert single["state"] == unknown["state"] == multiple["state"] == "active"
        self.assertEqual(single["rooms"], 1)
        self.assertTrue((single["address"] or "").endswith("квартира 25, комната 2"))
        self.assertIsNone(unknown["rooms"])
        self.assertEqual(multiple["rooms"], 2)
        self.assertIsNone(multiple["area_m2"])

        # Arrange
        info["roomArea"] = 9.0
        info["description"] = "Продаётся комната 9,2 м², на фото видна кухня."
        with self.assertRaises(RejectedCard) as rejected:
            # Act
            read_native(ROOM, data)

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_room_area")

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

    def test_house_subtypes_require_the_address_of_one_independent_unit(self) -> None:
        # Arrange: selected fields from the four native subtype observations.
        cases = (
            ("sale", "house_part", 1525510020, "Тольятти", "Рабочий проезд", None, 69),
            (
                "rent",
                "house_part",
                2081525526,
                "Тольятти",
                "КП Чайка, 23а",
                "Половина большого бревенчатого дома",
                250,
            ),
            (
                "sale",
                "townhouse",
                1695973117,
                "Самара",
                "Сарапульский переулок, 38",
                "Продается здание общежития. Также возможна продажа по комнатам.",
                394.1,
            ),
            (
                "rent",
                "townhouse",
                1885120006,
                "Чапаевск",
                "Херсонская улица, 7/2",
                "Сдается дом",
                140,
            ),
        )
        for deal, kind, identifier, locality, address, description, area in cases:
            with self.subTest(identifier=identifier):
                data = native_proposal()
                data.update(
                    {
                        "id": identifier,
                        "offerType": kind,
                        "dealType": deal,
                        "address": {
                            "displayName": f"Россия, Самарская область, {locality}, {address}",
                            "parents": [
                                {"kind": "locality", "name": locality},
                                {
                                    "kind": "province",
                                    "subkind": "region",
                                    "name": "Самарская область",
                                },
                            ],
                        },
                        "objectInfo": {"area": area, "description": description},
                    }
                )
                url = f"https://samara.domclick.ru/card/{deal}__{kind}__{identifier}"

                if deal == "rent" and kind == "townhouse":
                    # Act
                    result = read_native(url, data)

                    # Assert
                    assert result["state"] == "active"
                    self.assertEqual(result["property_type_id"], 3)
                    self.assertEqual(result["deal_type"], "month_rent")
                    self.assertEqual(result["area_m2"], Decimal("140.00"))
                    self.assertEqual(
                        result["address"],
                        "Самарская область, Чапаевск, Херсонская улица, 7/2",
                    )
                else:
                    # Act
                    with self.assertRaises(RejectedCard) as rejected:
                        read_native(url, data)

                    # Assert
                    self.assertEqual(
                        str(rejected.exception),
                        "unsupported_house_unit"
                        if kind == "townhouse"
                        else "unconfirmed_house_block",
                    )

    def test_addressable_duplex_with_conflicting_published_area_is_rejected(
        self,
    ) -> None:
        # Arrange: the comparable fields in the 2080868633 card observation.
        url = "https://samara.domclick.ru/card/sale__townhouse__2080868633"
        data = native_proposal()
        data.update(
            {
                "id": 2080868633,
                "offerType": "townhouse",
                "address": {
                    "displayName": "Самарская область, Янтарная улица, 5/2",
                    "parents": [
                        {
                            "kind": "locality",
                            "name": "коттеджный посёлок Union Village",
                        },
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {
                    "area": 105.3,
                    "description": "Одноэтажный дуплекс 100 м2.",
                },
            }
        )

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "conflicting_house_area")

    def test_zero_rooms_requires_an_explicit_flat_studio_title(self) -> None:
        # Arrange
        url = "https://samara.domclick.ru/card/sale__flat__2080269220"
        data = native_proposal()
        data.update({"id": 2080269220, "offerType": "flat", "objectInfo": {"rooms": 0}})
        script = ssr({"productCard": data})

        # Act
        result = parse_card(
            url,
            OBSERVED,
            [script],
            [],
            loaded_url=url,
            page_title="Купить квартиру-студию, 27 м² по адресу Самара",
        )
        with self.assertRaises(RejectedCard) as rejected:
            parse_card(
                url,
                OBSERVED,
                [script],
                [],
                loaded_url=url,
                page_title="Студия дизайна рядом",
            )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["rooms"], 0)
        self.assertEqual(str(rejected.exception), "unconfirmed_studio")

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
        empty = read_native(HOUSE, {}, removed=True)
        missing_id = read_native(HOUSE, {**data, "id": None}, removed=True)
        still_active = read_native(HOUSE, {**data, "status": 2}, removed=True)

        # Assert
        self.assertEqual(bare["state"], "unavailable")
        self.assertEqual(removed["state"], "removed")
        self.assertEqual(conflict["state"], "unavailable")
        self.assertEqual(empty["state"], "unavailable")
        self.assertEqual(missing_id["state"], "unavailable")
        self.assertEqual(still_active["state"], "active")

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

    def test_house_part_keeps_the_explicit_block_scope_and_number(self) -> None:
        # Arrange: a control using the accepted 6.3 address notation.
        url = "https://samara.domclick.ru/card/sale__house_part__7777777"
        data = native_proposal()
        data.update(
            {
                "id": 7777777,
                "offerType": "house_part",
                "address": {
                    "displayName": "Самара, Солнечная улица, 7, блок 2",
                    "parents": [
                        {"kind": "locality", "name": "Самара"},
                        {
                            "kind": "province",
                            "subkind": "region",
                            "name": "Самарская область",
                        },
                    ],
                },
                "objectInfo": {"area": 55, "description": "Самостоятельная секция."},
            }
        )

        # Act
        result = read_native(url, data)

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["area_m2"], Decimal("55.00"))
        self.assertEqual(
            result["address"], "Самарская область, Самара, Солнечная улица, 7, блок 2"
        )

        # Arrange: the same address belongs to a block whose sold share is unallocated.
        data["objectInfo"] = {
            "area": 55,
            "description": "Продаётся 1/2 доля в блоке 2. Самостоятельная часть доли не выделена.",
        }

        # Act
        with self.assertRaises(RejectedCard) as rejected:
            read_native(url, data)

        # Assert
        self.assertEqual(str(rejected.exception), "unallocated_house_share")

    def test_daily_studio_uses_published_unit_title_with_generic_page_title(
        self,
    ) -> None:
        # Arrange
        card = daily_projection()
        card.update({"rooms": 0, "title": "Квартира-студия"})

        # Act
        result = parse_card(
            DAILY,
            OBSERVED,
            [daily_ssr(card)],
            [],
            loaded_url=DAILY,
            page_title="Домклик24",
        )

        # Assert
        assert result["state"] == "active"
        self.assertEqual(result["rooms"], 0)

    def test_daily_studio_rejects_conflicting_public_unit_titles(self) -> None:
        for heading, reason in (
            ("Снять однокомнатную квартиру 36 м² посуточно", "contradictory_rooms"),
            ("Снять частный дом", "contradictory_daily_type"),
        ):
            with self.subTest(heading=heading):
                # Arrange
                card = daily_projection()
                card.update({"rooms": 0, "title": "Квартира-студия"})

                # Act
                with self.assertRaises(RejectedCard) as rejected:
                    parse_card(
                        DAILY,
                        OBSERVED,
                        [daily_ssr(card)],
                        [],
                        loaded_url=DAILY,
                        page_title=heading,
                    )

                # Assert
                self.assertEqual(str(rejected.exception), reason)
