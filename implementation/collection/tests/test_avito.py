import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast
from unittest import TestCase

from playwright.sync_api import Route, sync_playwright

from catalog.contracts import Deal, Snapshot
from collection.collectors.avito import parse_card, read_card
from collection.normalization import RejectedCard, as_object

FIXTURE = Path(__file__).with_name("fixtures") / "avito-6.1.json"
OBSERVED_AT = datetime(2026, 11, 18, 16, tzinfo=UTC)
DAILY_LABEL = "от 4 980 ₽ за 1 сутки"


class Projection(TypedDict):
    url: str
    title: str
    route: dict[str, object]


def projection(label: str) -> Projection:
    return cast(dict[str, Projection], json.loads(FIXTURE.read_text()))[label]


def item_for(card: Projection) -> dict[str, object]:
    return as_object(as_object(card["route"]["buyerItem"])["item"])


def parsed(card: Projection) -> Snapshot:
    return parse_card(
        card["url"],
        OBSERVED_AT,
        card["route"],
        loaded_url=card["url"],
        page_title=card["title"],
    )


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
            "room": (
                "7697142163",
                2,
                "sale",
                None,
                "12.50",
                None,
                1,
                3,
                None,
                "752777.00",
                "listed",
                None,
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
        cards = {label: projection(label) for label in [*cases, "closed"]}
        by_url = {card["url"]: card for card in cards.values()}
        requests: list[str] = []

        def serve_projection(route: Route) -> None:
            requests.append(route.request.url)
            card = by_url.get(route.request.url)
            if card is None:
                route.abort()
                return
            hydration = {"loaderData": {"catalog-or-main-or-item": card["route"]}}
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<title>{card['title']}</title><script>"
                f"window.__staticRouterHydrationData = {json.dumps(hydration)};"
                "</script>",
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
                results[label] = read_card(
                    page,
                    card["url"],
                    OBSERVED_AT,
                    expected_deal="sale" if label == "closed" else None,
                    listing_price=(card["url"], DAILY_LABEL)
                    if label == "day_rent"
                    else None,
                )

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

        # Act
        same = parse_card(
            url + "?utm=x#original",
            OBSERVED_AT,
            card["route"],
            loaded_url=url + "?context=y#after",
        )
        wrong_ids = [
            parse_card(url, OBSERVED_AT, card["route"], loaded_url=loaded)
            for loaded in (
                other_id,
                other_family,
                "https://avito.ru.example" + url[20:],
            )
        ]
        item_for(card)["id"] = 3638106727
        wrong_ids.append(parsed(card))
        with self.assertRaisesRegex(RejectedCard, "invalid_identity"):
            parse_card(url + "/extra", OBSERVED_AT, card["route"], loaded_url=url)

        # Assert
        assert same["state"] == "active"
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

    def test_missing_closed_signal_or_known_deal_is_unavailable(self) -> None:
        cases: tuple[tuple[dict[str, object], Deal | None], ...] = (
            ({"type": "notFound", "isGone": True}, "sale"),
            ({"type": "notFound", "message": "Объявление закрыто."}, None),
        )
        for route, expected_deal in cases:
            with self.subTest(route=route, expected_deal=expected_deal):
                # Arrange
                url = projection("closed")["url"]

                # Act
                snapshot = parse_card(
                    url, OBSERVED_AT, route, loaded_url=url, expected_deal=expected_deal
                )

                # Assert
                self.assertEqual(snapshot["state"], "unavailable")

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
        item["address"] = "Самарская обл.,  ул. Лесная, д. 7а, корп. А, кв. 12"
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
            "Самарская область, улица Лесная, д. 7а, корпус А, квартира 12",
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
            assert result["state"] == "active"
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
        assert snapshot["state"] == "active"
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

        # Act
        ordinary = parsed(card)
        ambiguous_share = parsed(projection("room"))

        # Assert
        assert ordinary["state"] == "active"
        assert ambiguous_share["state"] == "active"
        self.assertEqual(ordinary["rooms"], 1)
        self.assertIsNone(ambiguous_share["rooms"])
