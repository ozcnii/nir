import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from app.catalog.contracts import Snapshot, SnapshotResult
from app.collection.collectors.avito import capture_card, parse_card
from app.collection.normalization import RejectedCard, as_object
from app.collection.tests.avito_fixture import Projection, projection, projection_html

OBSERVED_AT = datetime(2026, 11, 18, 16, tzinfo=UTC)
DAILY_LABEL = "от 4 980 ₽ за 1 сутки"


def item_for(card: Projection) -> dict[str, object]:
    return as_object(as_object(card["route"]["buyerItem"])["item"])


def parsed(card: Projection) -> Snapshot:
    result = parse_card(
        card["url"],
        OBSERVED_AT,
        card["route"],
        loaded_url=card["url"],
        page_title=card["title"],
    )
    assert not isinstance(result, SnapshotResult)
    return result


class AvitoCardTests(TestCase):
    def test_saved_card_projections_through_chromium(self) -> None:
        # Arrange: replay observed formats with a reconstructed hydration envelope.
        cases = {
            "new_build": (
                "3638106726",
                1,
                "sale",
                "new_build",
                "48.10",
                2,
                11,
                12,
                None,
                "14766700.00",
                "listed",
                5,
            ),
            "resale": (
                "8199518030",
                1,
                "sale",
                "resale",
                "57.00",
                1,
                17,
                18,
                None,
                "9500000.00",
                "listed",
                None,
            ),
            "house": (
                "8457143068",
                3,
                "sale",
                None,
                "59.20",
                2,
                None,
                1,
                "600.00",
                "6250000.00",
                "listed",
                28,
            ),
            "month_rent": (
                "4977842672",
                1,
                "month_rent",
                None,
                "64.00",
                2,
                27,
                27,
                None,
                "43000.00",
                "listed",
                None,
            ),
            "day_rent": (
                "4595685717",
                1,
                "day_rent",
                None,
                "42.00",
                1,
                12,
                26,
                None,
                "4980.00",
                "from",
                None,
            ),
        }
        cards = {label: projection(label) for label in [*cases, "closed", "room"]}
        by_url = {card["url"]: card for card in cards.values()}
        requests: list[str] = []

        def serve_projection(route: Route) -> None:
            requests.append(route.request.url)
            card = by_url.get(route.request.url)
            if card is None:
                route.abort()
                return
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=projection_html(card),
            )

        # Act
        results = {}
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium", headless=True) as browser,
            browser.new_context() as context,
        ):
            context.route("**/*", serve_projection)
            page = context.new_page()
            for label, card in cards.items():
                page.goto(card["url"], wait_until="domcontentloaded")
                if label == "room":
                    # The saved card gives the area of a room but sells an unscoped share.
                    with self.assertRaisesRegex(
                        RejectedCard, "^unconfirmed_room_unit$"
                    ):
                        capture_card(
                            page, card["url"], OBSERVED_AT, time.monotonic() + 5
                        )
                    continue
                snapshot = capture_card(
                    page,
                    card["url"],
                    OBSERVED_AT,
                    time.monotonic() + 5,
                    removed_deal="sale" if label == "closed" else None,
                    listing_price=(card["url"], DAILY_LABEL)
                    if label == "day_rent"
                    else None,
                )
                assert not isinstance(snapshot, SnapshotResult)
                results[label] = snapshot

        # Assert: URL-only gallery; no request beyond the fixture documents.
        self.assertEqual(requests, list(by_url))
        for label, row in cases.items():
            with self.subTest(label=label):
                (
                    key,
                    kind,
                    deal,
                    segment,
                    area,
                    rooms,
                    floor,
                    floors,
                    land,
                    amount,
                    price_kind,
                    photo_count,
                ) = row
                snap = results[label]
                assert snap["state"] == "active"
                self.assertEqual(snap["source_code"], "avito")
                self.assertEqual(snap["external_id"], key)
                self.assertEqual(snap["observed_at"], OBSERVED_AT)
                self.assertEqual(snap["url"], cards[label]["url"])
                self.assertEqual(snap["region_id"], 68)
                self.assertEqual(snap["property_type_id"], kind)
                self.assertEqual(snap["deal_type"], deal)
                self.assertEqual(snap["market_segment"], segment)
                self.assertEqual(snap["area_m2"], Decimal(area))
                self.assertEqual(snap["rooms"], rooms)
                self.assertEqual(snap["floor"], floor)
                self.assertEqual(snap["floors_total"], floors)
                self.assertEqual(snap["land_area_m2"], Decimal(land) if land else None)
                self.assertEqual(
                    snap["price"],
                    {
                        "status": "known",
                        "price_rub": Decimal(amount),
                        "price_kind": price_kind,
                        "currency": "RUB",
                    },
                )
                if photo_count is None:
                    self.assertEqual(snap["photos"], {"status": "unavailable"})
                else:
                    photos = snap["photos"]
                    assert photos["status"] == "known"
                    self.assertEqual(len(photos["urls"]), photo_count)
                    gallery = cast(
                        list[dict[str, str]], item_for(cards[label])["imageUrls"]
                    )
                    self.assertEqual(
                        photos["urls"], [photo["1280x960"] for photo in gallery]
                    )
        self.assertEqual(
            results["closed"],
            {
                "source_code": "avito",
                "external_id": "8417769121",
                "state": "removed",
                "observed_at": OBSERVED_AT,
                "url": cards["closed"]["url"],
                "deal_type": "sale",
            },
        )

    def test_key_ignores_query_and_fragment_but_checks_full_url_and_item(self) -> None:
        # Arrange
        card = projection("new_build")
        url = card["url"]
        other_id = url.replace("3638106726", "3638106727")
        other_family = url.replace("kvartiry", "komnaty")
        wrong_item = projection("new_build")
        item_for(wrong_item)["id"] = 3638106727
        foreign_urls = (
            other_id,
            other_family,
            url.replace("www.avito.ru", "avito.ru.example", 1),
        )

        # Act
        same = parse_card(
            url + "?utm=x#original",
            OBSERVED_AT,
            card["route"],
            loaded_url=url + "?context=y#after",
        )
        wrong_ids = [
            parse_card(url, OBSERVED_AT, card["route"], loaded_url=loaded)
            for loaded in foreign_urls
        ]
        wrong_ids.append(parsed(wrong_item))
        with self.assertRaises(RejectedCard) as raised:
            parse_card(url + "/extra", OBSERVED_AT, card["route"], loaded_url=url)

        # Assert
        self.assertEqual(str(raised.exception), "invalid_identity")
        assert not isinstance(same, SnapshotResult) and same["state"] == "active"
        self.assertEqual(same["external_id"], "3638106726")
        self.assertEqual(same["url"], url + "?context=y#after")
        self.assertEqual(
            wrong_ids,
            [
                {
                    "state": "unavailable",
                    "source_code": "avito",
                    "external_id": "3638106726",
                    "observed_at": OBSERVED_AT,
                }
            ]
            * 4,
        )

    def test_only_exact_closure_for_same_unknown_key_returns_not_found(self) -> None:
        # Arrange
        card = projection("closed")
        url = card["url"]

        # Act
        closed = parse_card(url, OBSERVED_AT, card["route"], loaded_url=url)
        unsigned = parse_card(url, OBSERVED_AT, {"type": "notFound"}, loaded_url=url)
        redirected = parse_card(
            url,
            OBSERVED_AT,
            card["route"],
            loaded_url=url.replace("8417769121", "8417769122"),
        )

        # Assert
        self.assertEqual(closed, SnapshotResult("not_found"))
        assert not isinstance(unsigned, SnapshotResult)
        assert not isinstance(redirected, SnapshotResult)
        self.assertEqual(unsigned["state"], "unavailable")
        self.assertEqual(redirected["state"], "unavailable")

    def test_invalid_structural_units_and_unsupported_units_do_not_produce_p3(
        self,
    ) -> None:
        for label, title, field, value, error in (
            ("house", None, "Площадь участка", "6 акров", "invalid_area_unit"),
            ("house", None, "Площадь участка", "10.001 м²", "invalid_precision"),
            (
                "house",
                None,
                "Площадь участка",
                "6.240000000000000000000000000001 сот.",
                "invalid_precision",
            ),
            ("house", None, "Этажей в доме", "1–3", "invalid_number"),
            ("house", "Типовой проект дома", None, None, "unconfirmed_house_unit"),
            ("house", "Два дома одним лотом", None, None, "unconfirmed_house_unit"),
            ("house", "Дом 58 м²", None, None, "conflicting_house_measurement"),
            ("room", "Доля в квартире", None, None, "unconfirmed_room_unit"),
            ("room", "Номер люкс в гостинице", None, None, "unsupported_hotel_unit"),
        ):
            with self.subTest(label=label, field=field, value=value, title=title):
                # Arrange: mutate only one part of the observed card.
                card = projection(label)
                if title is not None:
                    item_for(card)["title"] = title
                fields = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
                for param in cast(list[dict[str, object]], fields["items"]):
                    if param["title"] == field:
                        param["description"] = value

                with self.assertRaises(RejectedCard) as raised:
                    # Act
                    parsed(card)

                # Assert
                self.assertEqual(str(raised.exception), error)

    def test_normalizes_exact_land_coords_address_and_preserves_seller_text(
        self,
    ) -> None:
        # Arrange: half-even ties with opposite parity and a signed photo URL.
        card = projection("house")
        item = item_for(card)
        item["geo"] = {"coords": {"lat": "53.12345665", "lng": "50.12345675"}}
        item["title"] = "Дом 59,2 м²"
        item["address"] = "Самарская обл.,  ул. Лесная, д. 7а, корп. А, стр. 12"
        item["description"] = "Первая строка.\n\nСкрыть недостатки? Нет."
        photo = {"1280x960": "https://image.invalid/one?size=large&key=abc"}
        item["imageUrls"] = [photo, photo]
        fields = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
        for param in cast(list[dict[str, object]], fields["items"]):
            if param["title"] == "Площадь участка":
                param["description"] = "6,24 сот."

        # Act
        snapshot = parsed(card)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["latitude"], Decimal("53.1234566"))
        self.assertEqual(snapshot["longitude"], Decimal("50.1234568"))
        self.assertEqual(
            snapshot["address"],
            "Самарская область, улица Лесная, д. 7а, корпус А, строение 12",
        )
        self.assertEqual(snapshot["description"], item["description"])
        self.assertEqual(snapshot["land_area_m2"], Decimal("624.00"))
        self.assertEqual(
            snapshot["photos"], {"status": "known", "urls": list(photo.values())}
        )

    def test_incomplete_price_evidence_and_gallery_are_unavailable(self) -> None:
        # Arrange: another card's price label, a deposit, or a confirmed empty gallery.
        card = projection("day_rent")
        url = card["url"]
        item = item_for(card)
        item["imageUrls"] = [{"1280x960": "https://image.invalid/one"}, {}]
        wrong_id = url.replace("4595685717", "4595685718")

        # Act
        results = [
            parse_card(
                url,
                OBSERVED_AT,
                card["route"],
                loaded_url=url,
                page_title=card["title"],
                listing_price=evidence,
            )
            for evidence in (None, (wrong_id, DAILY_LABEL), (url, "Залог 4 980 ₽"))
        ]
        item["imageUrls"] = []
        as_object(item["formattedPrice"])["formatedString"] = "от 4 980 ₽ за сутки"
        explicit = parsed(card)

        # Assert
        for result in results:
            assert (
                not isinstance(result, SnapshotResult) and result["state"] == "active"
            )
            self.assertEqual(result["photos"], {"status": "unavailable"})
            self.assertEqual(result["price"], {"status": "unavailable"})
        assert explicit["state"] == "active"
        self.assertEqual(explicit["photos"], {"status": "known", "urls": []})
        self.assertEqual(
            explicit["price"],
            {
                "status": "known",
                "price_rub": Decimal("4980.00"),
                "price_kind": "from",
                "currency": "RUB",
            },
        )

    def test_same_card_listing_from_qualifies_card_amount(self) -> None:
        # Arrange: listing has a different base amount for the same publication.
        card = projection("day_rent")
        url = card["url"]

        # Act
        snapshot = parse_card(
            url,
            OBSERVED_AT,
            card["route"],
            loaded_url=url,
            page_title=card["title"],
            listing_price=(url, "от 4 500 ₽ за 1 сутки"),
        )

        # Assert
        assert (
            not isinstance(snapshot, SnapshotResult) and snapshot["state"] == "active"
        )
        self.assertEqual(
            snapshot["price"],
            {
                "status": "known",
                "price_rub": Decimal("4980.00"),
                "price_kind": "from",
                "currency": "RUB",
            },
        )

    def test_conflicting_explicit_hotel_title_is_rejected(self) -> None:
        for channel in ("item", "page"):
            with self.subTest(channel=channel):
                # Arrange: the other title still names an apartment.
                card = projection("day_rent")
                item = item_for(card)
                item["title"] = "Квартира 42 м²"
                if channel == "item":
                    item["title"] = "Гостиничный номер люкс"
                else:
                    card["title"] = "Номер в апарт-отеле | Квартира 42 м²"

                with self.assertRaises(RejectedCard) as raised:
                    # Act
                    parsed(card)

                # Assert
                self.assertEqual(str(raised.exception), "unsupported_hotel_unit")

    def test_null_coordinates_and_malformed_pairs(self) -> None:
        # Arrange
        card = projection("new_build")
        item = item_for(card)
        item["geo"] = {"coords": {"lat": None, "lng": None}}

        # Act
        nulls = parsed(card)

        # Assert
        assert nulls["state"] == "active"
        self.assertIsNone(nulls["latitude"])
        self.assertIsNone(nulls["longitude"])
        malformed: tuple[object, ...] = (
            [],
            {},
            {"lat": None},
            {"lat": 53, "lng": None},
        )
        for coords in malformed:
            with self.subTest(coords=coords):
                # Arrange
                item["geo"] = {"coords": coords}

                with self.assertRaises(RejectedCard) as raised:
                    # Act
                    parsed(card)

                # Assert
                self.assertIn(
                    str(raised.exception),
                    {
                        "invalid_coordinates",
                        "invalid_coordinate_pair",
                    },
                )

    def test_malformed_parameter_row_is_rejected(self) -> None:
        # Arrange
        card = projection("new_build")
        params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
        rows = cast(list[dict[str, object]], params["items"])
        del rows[0]["title"]

        with self.assertRaises(RejectedCard) as raised:
            # Act
            parsed(card)

        # Assert
        self.assertEqual(str(raised.exception), "invalid_card_param")

    def test_room_count_requires_a_specific_offered_room(self) -> None:
        # Arrange: an ordinary sale explicitly names the room and its own area.
        card = projection("room")
        item_for(card)["title"] = "Комната 12,5 м² в 2-к."
        card["title"] = "Комната 12,5 м² в 2-к. на продажу"
        params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
        rows = cast(list[dict[str, object]], params["items"])
        params["items"] = [row for row in rows if row["title"] != "Условия продажи"]

        share = projection("room")
        item_for(share)["title"] = "Доля: одна комната 12,5 м²"
        item_for(card)["title"] = (
            "Комната 12,5 м² в 2-к. Отдельно можно купить вторую комнату"
        )

        # Act
        ordinary = parsed(card)
        physical_share = parsed(share)

        # Assert
        assert ordinary["state"] == "active"
        assert physical_share["state"] == "active"
        self.assertEqual(ordinary["rooms"], 1)
        self.assertEqual(physical_share["rooms"], 1)

    def test_wrong_display_amount_or_unit_is_unavailable(self) -> None:
        for label, display in (
            ("resale", "9 500 000 ₽ за м²"),
            ("resale", "9 500 001 ₽"),
            ("month_rent", "43 000 ₽ за сутки"),
        ):
            with self.subTest(label=label, display=display):
                # Arrange: numeric fields and postfix remain unchanged.
                card = projection(label)
                as_object(item_for(card)["formattedPrice"])["formatedString"] = display

                # Act
                snapshot = parsed(card)

                # Assert
                assert snapshot["state"] == "active"
                self.assertEqual(snapshot["price"], {"status": "unavailable"})

    def test_primary_room_title_and_structured_area_conflict(self) -> None:
        # Arrange
        card = projection("room")
        item_for(card)["title"] = "Доля: одна комната 12,5 м²"
        params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
        for row in cast(list[dict[str, object]], params["items"]):
            if row["title"] == "Площадь комнаты":
                row["description"] = "9 м²"

        with self.assertRaises(RejectedCard) as raised:
            # Act
            parsed(card)

        # Assert
        self.assertEqual(str(raised.exception), "conflicting_room_area")

    def test_multiple_offered_rooms_do_not_become_one(self) -> None:
        # Arrange: the title explicitly mentions a second offered room.
        card = projection("room")
        item_for(card)["title"] = "Комната 12,5 м² и ещё одна комната 10 м² одним лотом"
        card["title"] = "Комната 12,5 м² и комната 10 м² на продажу"
        params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
        rows = cast(list[dict[str, object]], params["items"])
        params["items"] = [row for row in rows if row["title"] != "Условия продажи"]

        # Act
        snapshot = parsed(card)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["rooms"], 2)
        self.assertIsNone(snapshot["area_m2"])

    def test_abstract_share_does_not_create_a_room(self) -> None:
        # Arrange
        card = projection("room")
        item_for(card)["title"] = "Доля 1/2 в квартире"
        with self.assertRaises(RejectedCard) as rejected:
            # Act
            parsed(card)

        # Assert
        self.assertEqual(str(rejected.exception), "unconfirmed_room_unit")

    def test_only_offered_bundle_and_its_total_are_used(self) -> None:
        cases = (
            ("Продаётся комната 12,5 м², № 2.", 1, "12.50"),
            ("Продаётся комната, № 2.", 1, "12.50"),
            ("Удобный район. Продаются две комнаты одним лотом.", 2, None),
            (
                "Продаются комнаты одним лотом, общей площадью 22,5 м².",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты одним лотом. Общая площадь этих комнат — 22,5 м².",
                None,
                "22.50",
            ),
            (
                "Общая площадь этих комнат — 22,5 м². "
                "Продаются комнаты общей площадью 22,5 м² одним лотом.",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты одним лотом. "
                "Общая площадь этих комнат 23 м² в соседней квартире.",
                None,
                None,
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "В соседней квартире две комнаты № 4 и № 5. "
                "Общая площадь этих комнат — 23 м².",
                None,
                "22.50",
            ),
            (
                "В соседней квартире две комнаты № 4 и № 5. "
                "Общая площадь этих комнат — 23 м². "
                "Продаются комнаты одним лотом.",
                None,
                None,
            ),
            (
                "Продаются комнаты одним лотом. "
                "Отдельно предлагаются комнаты № 4 и № 5. "
                "Общая площадь этих комнат — 23 м².",
                None,
                None,
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "Отдельно предлагаются комнаты № 4 и № 5. "
                "Общая площадь этих предлагаемых комнат — 23 м².",
                None,
                "22.50",
            ),
            (
                "Отдельно предлагаются комнаты № 4 и № 5. "
                "Общая площадь этих предлагаемых комнат — 23 м². "
                "Продаются комнаты общей площадью 22,5 м² одним лотом.",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты № 2 площадью 9 м² одним лотом, "
                "общей площадью 22,5 м². Продаётся комната № 2 в соседней квартире. "
                "Площадь предлагаемой комнаты № 2 — 11 м².",
                None,
                "22.50",
            ),
            (
                "В соседней квартире: продаётся комната № 2. "
                "Площадь предлагаемой комнаты № 2 — 11 м². "
                "Продаются комнаты № 2 площадью 9 м² одним лотом, "
                "общей площадью 22,5 м².",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты одним лотом. "
                "В соседней квартире две комнаты № 4 и № 5. "
                "Общая площадь этих предлагаемых комнат — 22,5 м².",
                None,
                None,
            ),
            (
                "Продаются комнаты одним лотом. "
                "В соседней квартире две комнаты № 4 и № 5. "
                "Общая площадь предлагаемых комнат — 22,5 м².",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "Продаются комнаты общей площадью 23 м² одним лотом в соседней квартире.",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты одним лотом. "
                "В соседней квартире:\nпродаются комнаты общей площадью 23 м². "
                "Общая площадь этих комнат — 23 м².",
                None,
                None,
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "В другом объявлении указаны две комнаты. "
                "Общая площадь этих комнат — 23 м².",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "Общая площадь предлагаемых комнат 23 м² в соседней квартире. "
                "Общая площадь этих комнат — 23 м².",
                None,
                "22.50",
            ),
            (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "Отдельно предлагаются комнаты № 4 и № 5. "
                "В соседней квартире две комнаты № 4 и № 5. "
                "Общая площадь предлагаемых комнат — 23 м².",
                None,
                "22.50",
            ),
            (
                "Общая площадь этих комнат — 22,5 м². "
                "Продаётся комната № 2 площадью 12,5 м².",
                1,
                "12.50",
            ),
            (
                "Продаются комната 12,5 м², комната 10 м², общей площадью 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продаются комната № 2 и комната № 3 одним лотом, "
                "общей площадью 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продаются две комнаты одним лотом. "
                "Общая площадь предлагаемых комнат 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продаются комната № 2 и комната № 2 одним лотом, "
                "общей площадью 12,5 м².",
                1,
                "12.50",
            ),
            (
                "Удобный район. Продаются две комнаты общей площадью 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продам две комнаты № 2 и № 3 одним лотом, общей площадью 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продам комнаты № 2 и № 3 одним лотом, общей площадью 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продам комнату № 2 общей площадью 12,5 м² и комнату № 3 "
                "общей площадью 10 м² одним лотом.",
                2,
                None,
            ),
            (
                "Продам комнату № 2 площадью 12,5 м² и комнату № 3 "
                "площадью 10 м² по отдельности.",
                1,
                "12.50",
            ),
            (
                "Продам комнату № 2 площадью 12,5 м² и комнату № 3 "
                "площадью 10 м² одним лотом. Общая площадь обеих комнат 22,5 м².",
                2,
                "22.50",
            ),
            (
                "Продам комнату № 2 площадью 9 м² и комнату № 3 площадью 10 м² "
                "одним лотом. Площадь предлагаемой комнаты № 2 9 м².",
                2,
                None,
            ),
            (
                "Продам комнату № 2 площадью 9 м² и комнату № 3 площадью 10 м² "
                "одним лотом. Площадь соседской комнаты № 4 11 м².",
                2,
                None,
            ),
            (
                "Кухня площадью 7 м². Комната соседей 15 м². "
                "Отдельно можно купить вторую комнату.",
                1,
                "12.50",
            ),
        )
        for description, count, area in cases:
            with self.subTest(description=description):
                # Arrange
                card = projection("room")
                item_for(card).update(title="Комната 12,5 м²", description=description)
                card["title"] = "Комната 12,5 м² на продажу"
                params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
                params["items"] = [
                    row
                    for row in cast(list[dict[str, object]], params["items"])
                    if row["title"] != "Условия продажи"
                ]

                # Act
                snapshot = parsed(card)

                # Assert
                assert snapshot["state"] == "active"
                self.assertEqual(snapshot["rooms"], count)
                self.assertEqual(
                    snapshot["area_m2"], Decimal(area) if area is not None else None
                )

    def test_scoped_conflicts_are_detected_after_seller_introduction(self) -> None:
        cases = (
            (
                "room",
                "Комната 12,5 м²",
                "Продам две комнаты № 2 и № 2 одним лотом, общей площадью 22,5 м².",
                "conflicting_room_count",
            ),
            (
                "room",
                "Доля: одна комната 12,5 м²",
                "Квартира светлая. Площадь предлагаемой комнаты 9,2 м².",
                "conflicting_room_area",
            ),
            (
                "room",
                "Доля: комната № 2 12,5 м²",
                "Квартира светлая. Доля не выделена. Комната № 2 принадлежит соседям.",
                "unconfirmed_room_unit",
            ),
            (
                "room",
                "Доля: комната № 2 12,5 м²",
                "Рядом школа — площадь предлагаемой комнаты № 2 9,2 м².",
                "conflicting_room_area",
            ),
            (
                "room",
                "Доля: комната № 2 12,5 м²",
                "Показы по записи. Комната № 2 — соседская.",
                "unconfirmed_room_unit",
            ),
            (
                "room",
                "Доля: комната № 2 12,5 м²",
                "Этой доле не соответствует комната № 2.",
                "unconfirmed_room_unit",
            ),
            (
                "house",
                None,
                "Рядом лес. Продаются два дома одним лотом.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Предлагается дом 59,2 м² и второй дом 40 м² одним лотом.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Продаётся дом площадью 59,2 м² и самостоятельный блок № 2 "
                "площадью 40 м² одним лотом.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Продаётся дом площадью 59,2 м² и самостоятельный блок № 2 "
                "площадью 40 м² в соседнем доме одним лотом.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Рядом лес, площадь участка: 5 соток.",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Продаётся дом 59,2 м² на участке 5 соток.",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Рядом лес. Продаётся половина дома без выделения.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Рядом лес. Общая площадь дома 62 м².",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Рядом лес. Продаётся только блок № 2 этого дома.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Продаётся дом, точнее блок № 2 этого дома.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Площадь дома 59,2 м² — площадь участка 5 соток.",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Жилая площадь дома 38 м² — общая площадь дома 62 м².",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Продаётся дом, точнее — блок № 2 этого дома.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Продам дом площадью 59,2 м², второй дом площадью 40 м² "
                "и третий дом площадью 50 м² одним лотом.",
                "unconfirmed_house_unit",
            ),
            (
                "house",
                None,
                "Площадь дома 59,2 м² — площадь предлагаемого участка 5 соток.",
                "conflicting_house_measurement",
            ),
            (
                "house",
                None,
                "Площадь этого участка 5 соток — площадь дома 59,2 м².",
                "conflicting_house_measurement",
            ),
        )
        for family, title, description, reason in cases:
            with self.subTest(description=description):
                # Arrange
                card = projection(family)
                item_for(card)["description"] = description
                if title is not None:
                    item_for(card)["title"] = title

                with self.assertRaises(RejectedCard) as raised:
                    # Act
                    parsed(card)

                # Assert
                self.assertEqual(str(raised.exception), reason)

    def test_house_does_not_promote_an_addressed_block_to_whole_house(self) -> None:
        # Arrange
        card = projection("house")
        item = item_for(card)
        item["address"] = f"{item['address']}, дом 7 блок 2"
        item["description"] = "Рядом лес."

        with self.assertRaises(RejectedCard) as rejected:
            # Act
            parsed(card)

        # Assert
        self.assertEqual(str(rejected.exception), "unconfirmed_house_unit")

    def test_room_bundle_rejects_conflicting_owned_measurements(self) -> None:
        offer = (
            "Продам комнату № 2 площадью 9 м² и комнату № 3 площадью 10 м² одним лотом."
        )
        revised = offer.replace("9 м²", "11 м²")
        cases = (
            "Продаются комнаты общей площадью 22,5 м² одним лотом. "
            "Общая площадь этих комнат — 23 м².",
            "Продаются комнаты общей площадью 22,5 м² одним лотом. "
            "В соседней квартире две комнаты № 4 и № 5. "
            "Общая площадь предлагаемых комнат — 23 м².",
            "Общая площадь этих комнат — 23 м². "
            "Продаются комнаты общей площадью 22,5 м² одним лотом.",
            "Продаются комнаты общей площадью 22,5 м² одним лотом. "
            "Продаются комнаты № 2 и № 3 общей площадью 23 м² одним лотом.",
            "Продаются комнаты № 2 и № 3 общей площадью 23 м² одним лотом. "
            "Продаются комнаты общей площадью 22,5 м² одним лотом.",
            "Продаются две комнаты общей площадью 22,5 м². "
            "Общая площадь предлагаемых комнат 23 м².",
            "Продаются две комнаты общей площадью 22,5 м². "
            "Общая площадь этих двух комнат 23 м².",
            "Продаются комната № 2 и комната № 3 общей площадью 22,5 м². "
            "Общая площадь этих комнат 23 м².",
            "Общая площадь обеих комнат — 23 м². "
            "Продаются комната № 2 и комната № 3 общей площадью 22,5 м².",
            "Продаются комната № 2 площадью 12,5 м², комната № 3 площадью 10 м² "
            "и комната № 2 площадью 11 м² одним лотом.",
            "Продаются две комнаты общей площадью 22,5 м² и общей площадью 23 м².",
            "Продаются комната № 2 и комната № 2 одним лотом, общей площадью 22,5 м².",
            f"{offer} Площадь предлагаемой комнаты № 2 11 м².",
            f"Площадь предлагаемой комнаты № 2 11 м². {offer}",
            f"{offer} Продаётся комната № 2 в соседней квартире. "
            f"Площадь предлагаемой комнаты № 2 9 м². {offer} "
            "Площадь предлагаемой комнаты № 2 11 м².",
            f"{offer} {revised}",
            f"{revised} {offer}",
        )
        for description in cases:
            with self.subTest(description=description):
                # Arrange
                card = projection("room")
                item_for(card).update(title="Комната 12,5 м²", description=description)
                card["title"] = "Комната 12,5 м² на продажу"
                params = as_object(as_object(card["route"]["buyerItem"])["paramsDto"])
                params["items"] = [
                    row
                    for row in cast(list[dict[str, object]], params["items"])
                    if row["title"] != "Условия продажи"
                ]

                with self.assertRaises(RejectedCard) as rejected:
                    # Act
                    parsed(card)

                # Assert
                self.assertEqual(str(rejected.exception), "conflicting_room_area")

    def test_house_ignores_living_measurement_and_unrelated_second_building(
        self,
    ) -> None:
        # Arrange
        card = projection("house")
        item_for(card)["description"] = (
            "Продаётся дом 59,2 м² на участке 6 соток, жилая площадь дома 38 м². "
            "Площадь дома 59,2 м² — площадь участка 6 соток. "
            "Площадь дома 38 м² — жилая; общая площадь дома 59,2 м². "
            "Площадь дома 38 м² — жилая площадь. "
            "Площадь дома 38 м² (жилая); общая площадь дома 59,2 м². "
            "Предлагается дом 38 м² — жилая площадь. "
            "Второй дом на участке остаётся у владельца. "
            "Жилой блок остаётся у соседей. "
            "Для сравнения, продаётся блок № 2 в соседнем доме."
        )

        # Act
        snapshot = parsed(card)

        # Assert
        assert snapshot["state"] == "active"
        self.assertEqual(snapshot["area_m2"], Decimal("59.20"))
        self.assertEqual(snapshot["land_area_m2"], Decimal("600.00"))

    def test_browser_projection_preserves_malformed_geo(self) -> None:
        # Arrange: serve only the local replay document at the public card URL.
        card = projection("new_build")
        item_for(card)["geo"] = []
        html = projection_html(card)

        def serve_projection(route: Route) -> None:
            if route.request.url == card["url"]:
                route.fulfill(body=html, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        # Act
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium", headless=True) as browser,
            browser.new_context() as context,
        ):
            context.route("**/*", serve_projection)
            page = context.new_page()
            page.goto(card["url"], wait_until="domcontentloaded")
            with self.assertRaises(RejectedCard) as raised:
                capture_card(page, card["url"], OBSERVED_AT, time.monotonic() + 5)

        # Assert
        self.assertEqual(str(raised.exception), "invalid_geo")
