"""Run P3 acceptance through the actual PostgreSQL app role in a fixture DB."""

import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from unittest import TestCase, main

import django
import psycopg
from psycopg import Connection
from psycopg.rows import TupleRow

from catalog.contracts import ActiveSnapshot, KnownPrice

START = datetime(2026, 11, 18, 12, tzinfo=UTC)
PHOTO = "https://images.example.test/card.jpg?size=40"


@contextmanager
def as_app() -> Iterator[Connection[TupleRow]]:
    with psycopg.connect(
        dbname=os.environ["DB_NAME"],
        user="app",
        password=os.environ["DB_APP_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        autocommit=True,
    ) as connection:
        assert connection.execute(
            "SELECT current_database(), current_user, session_user, current_setting('server_version_num')"
        ).fetchone() == ("t15_catalog_test", "app", "app", "170012")
        yield connection


def price(amount: str = "8000000") -> KnownPrice:
    return {
        "status": "known",
        "price_rub": Decimal(amount),
        "price_kind": "listed",
        "currency": "RUB",
    }


def card(identifier: str) -> ActiveSnapshot:
    return {
        "source_code": "cian",
        "external_id": identifier,
        "observed_at": START,
        "state": "active",
        "url": f"https://samara.cian.ru/sale/flat/{identifier}/",
        "region_id": 68,
        "property_type_id": 1,
        "deal_type": "sale",
        "market_segment": "new_build",
        "address": "Самара",
        "latitude": None,
        "longitude": None,
        "area_m2": Decimal("47.21"),
        "rooms": 1,
        "floor": 2,
        "floors_total": 18,
        "land_area_m2": None,
        "description": None,
        "price": price(),
        "photos": {"status": "known", "urls": [PHOTO]},
    }


def later(source: ActiveSnapshot, minute: int) -> ActiveSnapshot:
    return {**source, "observed_at": START + timedelta(minutes=minute)}


def state(source: ActiveSnapshot, value: str, minute: int = 1) -> Mapping[str, object]:
    key = {
        "source_code": source["source_code"],
        "external_id": source["external_id"],
        "observed_at": START + timedelta(minutes=minute),
        "state": value,
    }
    if value == "removed":
        return {**key, "url": source["url"], "deal_type": source["deal_type"]}
    return key


class SnapshotTests(TestCase):
    def setUp(self) -> None:
        from catalog.services import accept_snapshot

        self.accept = accept_snapshot

    def test_matching_apartment_numbers_and_unique_candidate(self) -> None:
        # Arrange: one exact A match and two contradictory existing room counts.
        address = "Самарская область, Самара, ул. Гагарина, 17а, кв. 25"
        first = {**card("950000001"), "address": address, "rooms": 1}
        common = {
            **card("950000002"),
            "source_code": "domclick",
            "address": "Самара, Гагарина улица, дом 17а, квартира 25",
            "rooms": None,
        }
        other_rooms = {**first, "external_id": "950000003", "rooms": 3}
        multiple = {**common, "external_id": "950000004"}
        other_flat = {
            **first,
            "external_id": "950000005",
            "address": address.replace("кв. 25", "кв. 26"),
        }
        no_known_traits = {
            **first,
            "external_id": "950000006",
            "rooms": None,
            "area_m2": None,
            "floor": None,
        }
        with as_app() as connection:
            # Act
            a, b, c, d, e, f = (
                self.accept(connection, data)
                for data in (
                    first,
                    common,
                    other_rooms,
                    multiple,
                    other_flat,
                    no_known_traits,
                )
            )

            # Assert
            self.assertEqual(a.property_id, b.property_id)
            self.assertNotEqual(a.ad_id, b.ad_id)
            self.assertEqual(
                len(
                    {
                        a.property_id,
                        c.property_id,
                        d.property_id,
                        e.property_id,
                        f.property_id,
                    }
                ),
                5,
            )

    def test_matching_description_patch_and_preserved_originals(self) -> None:
        # Arrange: published address/measurements of the F07 pair, with synthetic
        # seller wording. The optional city district is present on only one side.
        original_address = "Самарская область, Самара, р-н Ленинский, ул. Буянова, 120"
        short_address = "Самара, улица Буянова, 120"
        source: ActiveSnapshot = {
            **card("950000010"),
            "address": original_address,
            "area_m2": Decimal("110.00"),
            "rooms": None,
            "floor": 12,
            "floors_total": 20,
            "description": "Кухня-гостиная 28 м². Спальня 24 м².",
        }
        incoming: ActiveSnapshot = {
            **card("950000011"),
            "source_code": "domclick",
            "address": f"{short_address}, квартира 25",
            "area_m2": Decimal("110"),
            "rooms": 3,
            "floor": 12,
            "floors_total": 20,
            "description": "⭐ КУХНЯ-ГОСТИНАЯ 28 м².\n\nСпальня 24 м².",
        }
        another = {
            **incoming,
            "external_id": "950000012",
            "address": f"{short_address}, квартира 26",
            "area_m2": Decimal("111"),
        }
        numberless = {**incoming, "external_id": "950000013", "address": short_address}
        empty = {**numberless, "external_id": "950000014", "description": None}
        repeat = {**later(incoming, 1), "address": short_address, "rooms": None}
        shifted = {**later(incoming, 2), "address": another["address"]}

        with as_app() as connection:
            # Act
            first = self.accept(connection, source)
            with self.assertRaises(psycopg.errors.CheckViolation):
                self.accept(connection, {**incoming, "price": price("12345")})
            rolled_back = connection.execute(
                "SELECT address, rooms FROM properties WHERE id=%s",
                (first.property_id,),
            ).fetchone()
            attached = self.accept(connection, incoming)
            separate = [
                self.accept(connection, data) for data in (another, numberless, empty)
            ]
            preserved = self.accept(connection, repeat)
            stale = self.accept(
                connection, {**shifted, "observed_at": START - timedelta(minutes=1)}
            )
            conflict = self.accept(connection, shifted)
            rows = connection.execute(
                """
                SELECT address, rooms, observed_at
                FROM properties JOIN ads ON ads.property_id=properties.id
                WHERE ads.id=%s
                """,
                (attached.ad_id,),
            ).fetchone()

            # Assert
            self.assertEqual(rolled_back, (original_address, None))
            self.assertEqual(first.property_id, attached.property_id)
            self.assertEqual(
                len({first.property_id, *(item.property_id for item in separate)}), 4
            )
            self.assertEqual(preserved.property_id, first.property_id)
            self.assertEqual((stale.outcome, conflict.outcome), ("stale", "conflict"))
            self.assertEqual(
                rows, (f"{original_address}, квартира 25", 3, repeat["observed_at"])
            )

    def test_create_repeat_and_price_state_changes(self) -> None:
        # Arrange
        source = card("940000001")
        with as_app() as connection:
            unchanged = later(source, 1)
            unchanged["area_m2"] = Decimal("48")
            qualifier = later(source, 2)
            qualifier["price"] = {**price(), "price_kind": "from"}
            changed = later(source, 3)
            changed["price"] = price("8000001")
            returned = later(source, 4)

            # Act
            receipts = [
                self.accept(connection, snap)
                for snap in (source, unchanged, qualifier, changed, returned)
            ]
            ad = connection.execute(
                """
                SELECT ads.id, property_id, observed_at, photos, photos_observed_at, area_m2
                FROM ads JOIN properties ON properties.id=ads.property_id
                WHERE source_code='cian' AND external_id=%s
                """,
                (source["external_id"],),
            ).fetchone()
            history = connection.execute(
                "SELECT price_rub, price_kind FROM price_history WHERE ad_id=%s ORDER BY observed_at",
                (receipts[0].ad_id,),
            ).fetchall()

            # Assert
            self.assertEqual(
                [r.outcome for r in receipts],
                [
                    "created",
                    "unchanged",
                    "updated",
                    "updated",
                    "updated",
                ],
            )
            self.assertEqual(
                [r.price_changed for r in receipts], [True, False, True, True, True]
            )
            identities = {(r.ad_id, r.property_id) for r in receipts}
            self.assertEqual(len(identities), 1)
            self.assertEqual(
                ad,
                (
                    receipts[0].ad_id,
                    receipts[0].property_id,
                    returned["observed_at"],
                    [PHOTO],
                    returned["observed_at"],
                    Decimal("47.21"),
                ),
            )
            self.assertEqual(
                history,
                [
                    (Decimal("8000000"), "listed"),
                    (Decimal("8000000"), "from"),
                    (Decimal("8000001"), "listed"),
                    (Decimal("8000000"), "listed"),
                ],
            )

    def test_time_precedes_incompatible_deal_and_missing_payload(self) -> None:
        # Arrange
        source = card("940000002")
        with as_app() as connection:
            created = self.accept(connection, source)
            receipts_input = []
            for minute in (-1, 0, 1):
                conflicting = later(source, minute)
                conflicting["deal_type"] = "month_rent"
                receipts_input.append(conflicting)
            invalid = later(source, 2)
            invalid["floor"] = 20
            invalid_text = [
                {**later(source, 2), "description": value}
                for value in ("broken\x00text", "broken\ud800text")
            ]
            invalid_photo = {
                **later(source, 2),
                "photos": {
                    "status": "known",
                    "urls": ["https://cdn.example.test:bad/a.jpg"],
                },
            }
            invalid_removal = dict(state(source, "removed", 2))
            del invalid_removal["url"]
            incompatible_removal = {
                **state(source, "removed", 2),
                "deal_type": "month_rent",
            }

            # Act
            results = [
                self.accept(connection, snap)
                for snap in (
                    *receipts_input,
                    invalid,
                    *invalid_text,
                    invalid_photo,
                    invalid_removal,
                    incompatible_removal,
                    state(source, "unavailable"),
                )
            ]
            ad = connection.execute(
                "SELECT deal_type, observed_at, is_active, photos FROM ads WHERE id=%s",
                (created.ad_id,),
            ).fetchone()

            # Assert
            self.assertEqual(
                [result.outcome for result in results],
                [
                    "stale",
                    "ignored",
                    "conflict",
                    "rejected",
                    "rejected",
                    "rejected",
                    "rejected",
                    "rejected",
                    "conflict",
                    "unavailable",
                ],
            )
            conflict = results[2]
            self.assertEqual(conflict.reason, "deal_type_changed")
            self.assertEqual(
                (conflict.previous_deal_type, conflict.incoming_deal_type),
                ("sale", "month_rent"),
            )
            self.assertEqual(ad, ("sale", START, True, [PHOTO]))

    def test_removed_and_photo_states_preserve_identity_and_history(self) -> None:
        # Arrange
        source = card("940000003")
        missing_photo = later(source, 1)
        missing_photo["price"] = {"status": "unavailable"}
        missing_photo["photos"] = {"status": "unavailable"}
        empty_photo = later(missing_photo, 3)
        empty_photo["photos"] = {"status": "known", "urls": []}
        with as_app() as connection:
            # Act
            missing_ad = self.accept(connection, state(source, "removed"))
            created = self.accept(connection, source)
            unchanged = self.accept(connection, missing_photo)
            removed = self.accept(connection, state(source, "removed", 2))
            middle = connection.execute(
                "SELECT is_active, photos, photos_observed_at FROM ads WHERE id=%s",
                (created.ad_id,),
            ).fetchone()
            restored = self.accept(connection, empty_photo)
            final = connection.execute(
                "SELECT is_active, photos, photos_observed_at FROM ads WHERE id=%s",
                (created.ad_id,),
            ).fetchone()
            history = connection.execute(
                "SELECT count(*) FROM price_history WHERE ad_id=%s", (created.ad_id,)
            ).fetchone()

            # Assert
            self.assertEqual(missing_ad.outcome, "not_found")
            self.assertEqual(unchanged.outcome, "unchanged")
            self.assertEqual(removed.outcome, "updated")
            self.assertEqual(middle, (False, [PHOTO], START))
            self.assertEqual(restored.outcome, "updated")
            self.assertEqual(
                (restored.ad_id, restored.property_id),
                (created.ad_id, created.property_id),
            )
            self.assertEqual(final, (True, [], empty_photo["observed_at"]))
            self.assertEqual(history, (1,))

    def test_price_insert_error_rolls_back_both_parent_rows(self) -> None:
        # Arrange
        source = card("940000004")
        source["price"] = price("12345")
        with as_app() as connection:
            before = connection.execute("SELECT count(*) FROM properties").fetchone()

            with self.assertRaises(psycopg.errors.CheckViolation) as failure:
                # Act
                self.accept(connection, source)

            # Assert
            self.assertEqual(failure.exception.diag.constraint_name, "t15_fail_fixture")
            self.assertEqual(
                connection.execute("SELECT count(*) FROM properties").fetchone(), before
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM ads WHERE source_code='cian' AND external_id=%s",
                    (source["external_id"],),
                ).fetchone()
            )

    def test_concurrent_repeats_serialize_on_real_database(self) -> None:
        # Arrange
        source = card("940000005")
        ready = Barrier(2)

        def submit() -> tuple[str, int | None, int | None]:
            with as_app() as connection:
                ready.wait(timeout=5)
                result = self.accept(connection, source)
                return result.outcome, result.ad_id, result.property_id

        # Act
        with ThreadPoolExecutor(max_workers=2) as callers:
            calls = [callers.submit(submit), callers.submit(submit)]
            results = [call.result(timeout=10) for call in calls]
        with as_app() as connection:
            count = connection.execute(
                "SELECT count(*) FROM price_history WHERE ad_id=%s", (results[0][1],)
            ).fetchone()

        # Assert
        self.assertEqual(
            sorted(result[0] for result in results), ["created", "ignored"]
        )
        self.assertEqual(results[0][1:], results[1][1:])
        self.assertEqual(count, (1,))


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    main(verbosity=2)
