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
from django.db import IntegrityError
from django.db import connection as django_connection
from fixture_database import require_fixture_database
from psycopg import Connection
from psycopg.rows import TupleRow

from app.catalog.contracts import ActiveSnapshot, KnownPrice, SnapshotResult

START = datetime(2026, 11, 18, 12, tzinfo=UTC)
PHOTO = "https://images.example.test/card.jpg?size=40"


@contextmanager
def as_app() -> Iterator[Connection[TupleRow]]:
    with django_connection.cursor() as cursor:
        require_fixture_database(cursor)
    # Use the Django-managed session for setup and independent SQL assertions.
    try:
        yield django_connection.connection
    finally:
        django_connection.close()


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
    def test_active_collection_blocks_another_run_and_reference_deletion(self) -> None:
        from app.catalog.locks import CollectionBusy, run_session
        from app.catalog.models import Region
        from app.catalog.references import delete_references

        # Arrange
        with as_app():
            reference_id = Region.objects.create(name="Test collection lock").pk

        def compete() -> None:
            with self.subTest(operation="second collection"):
                with self.assertRaises(CollectionBusy), run_session():
                    self.fail("A second collection started")
            try:
                with self.subTest(operation="delete unused reference"):
                    with self.assertRaises(CollectionBusy):
                        delete_references("regions", [reference_id])
            finally:
                django_connection.close()

        # Act: contention must use independent PostgreSQL sessions.
        with run_session(), ThreadPoolExecutor(max_workers=1) as callers:
            callers.submit(compete).result(timeout=10)
        # Act
        deleted = delete_references("regions", [reference_id])

        # Assert
        self.assertEqual(deleted, 1)

    def setUp(self) -> None:
        from app.catalog.publications import accept_snapshot

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
        with as_app():
            # Act
            a, b, c, d, e, f = (
                self.accept(data)
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

    def test_matching_leaves_unattributed_addresses_and_sizes_separate(self) -> None:
        cases = (
            ("Самарская область, Кировский р-н., ул. Садовая, 17, кв. 25", None),
            ("Самара, ул. Гагарина, ул. Садовая, 17, кв. 25", None),
            (
                "Самара, ул. Садовая, 17",
                "На фото соседняя квартира. Кухня 28 м²; спальня 24 м².",
            ),
        )
        with as_app():
            for index, (address, description) in enumerate(cases):
                with self.subTest(address=address):
                    # Arrange: identical offers whose address or sizes lack attribution.
                    first = {
                        **card(f"95000002{index}"),
                        "address": address,
                        "description": description,
                    }
                    second = {**first, "source_code": "domclick"}

                    # Act
                    left = self.accept(first)
                    right = self.accept(second)

                    # Assert
                    self.assertEqual(
                        (left.outcome, right.outcome), ("created", "created")
                    )
                    self.assertNotEqual(left.property_id, right.property_id)

    def test_matching_preserves_the_first_supported_settlement_and_district(
        self,
    ) -> None:
        # Arrange: the second source supplies qualifiers absent in the first.
        first = {**card("950000030"), "address": "Самара, ул. Лесная, 17, кв. 25"}
        qualified = "г. Самара, Кировский р-н., ул. Лесная, 17, кв. 25"
        second = {**card("950000031"), "address": qualified, "source_code": "domclick"}
        other = {
            **card("950000032"),
            "address": qualified.replace("Кировский р-н.", "Ленинский район"),
        }
        with as_app() as connection:
            # Act
            original = self.accept(first)
            attached = self.accept(second)
            separate = self.accept(other)
            saved = connection.execute(
                "SELECT address FROM properties WHERE id=%s", (original.property_id,)
            ).fetchone()

            # Assert
            self.assertEqual(attached.property_id, original.property_id)
            self.assertNotEqual(separate.property_id, original.property_id)
            self.assertEqual(saved, (qualified,))

    def test_template_filter_checks_the_same_house_before_comparing_unit_numbers(
        self,
    ) -> None:
        cases = (
            ("с. Лесное", "пос. Лесное", "ул. Молодежная", True),
            ("Самара, Кировский район", "Самара, Ленинский район", "ул. Лесная", True),
            ("с. Лесное", "с. Лесное", "ул. Садовая", False),
        )
        seller = "Кухня 28 м²; спальня 24 м²."
        with as_app():
            for index, (locality, other, street, should_attach) in enumerate(cases):
                with self.subTest(locality=locality, other=other, street=street):
                    # Arrange: the numbered competitor differs in floor;
                    # it can only affect the test through the template rule.
                    house = f"{locality}, {street}, 17"
                    first = {
                        **card(f"95000400{index}"),
                        "address": f"{house}, кв. 25",
                        "description": seller,
                    }
                    competitor = {
                        **first,
                        "external_id": f"95000401{index}",
                        "address": f"{other}, {street}, 17, кв. 26",
                        "floor": 3,
                    }
                    another = {
                        **competitor,
                        "external_id": f"95000403{index}",
                        "address": f"{other}, {street}, 17, кв. 27",
                        "floor": 4,
                    }
                    incoming = {
                        **first,
                        "external_id": f"95000402{index}",
                        "source_code": "domclick",
                        "address": house,
                    }

                    # Act
                    known = self.accept(first)
                    self.accept(competitor)
                    self.accept(another)
                    matched = self.accept(incoming)

                    # Assert
                    self.assertEqual(
                        matched.property_id == known.property_id, should_attach
                    )

    def test_a_and_b_remain_ambiguous_with_a_numbered_offer_in_another_house(
        self,
    ) -> None:
        # Arrange: creation without a description keeps the two local objects distinct.
        house = "Самара, Кировский район, ул. Садовая, 105"
        seller = "Кухня 28 м², спальня 24 м²."
        with_number = {
            **card("950006001"),
            "address": f"{house}, кв. 25",
            "description": seller,
        }
        without_number: ActiveSnapshot = {
            **card("950006002"),
            "address": house,
        }
        other_district = {
            **card("950006003"),
            "address": "Самара, Ленинский район, ул. Садовая, 105, кв. 26",
            "description": seller,
            "floor": 3,
        }
        incoming = {
            **card("950006004"),
            "address": "Самара, ул. Садовая, 105, кв. 25",
            "description": seller,
        }
        with as_app():
            # Act
            a = self.accept(with_number)
            b = self.accept(without_number)
            self.accept({**later(without_number, 1), "description": seller})
            self.accept(other_district)
            result = self.accept(incoming)

            # Assert
            self.assertEqual(result.outcome, "created")
            self.assertEqual(len({a.property_id, b.property_id, result.property_id}), 3)

    def test_b_uses_only_complete_layout_claims_and_the_full_seller_text(self) -> None:
        layout = "Кухня 28 м², спальня 24 м²"
        cases = (
            (
                "Кухня 28,5 м²,\nспальня 24 м².",
                "Кухня 28.5 м², спальня 24 м².",
                True,
            ),
            (f"{layout}, обе отсутствуют.", f"{layout}, обе отсутствуют.", False),
            (
                f"Планировка: {layout}. Эти помещения не требуют ремонта.",
                f"Планировка: {layout}. Эти помещения не требуют ремонта.",
                False,
            ),
            (
                f"{layout}. Эти помещения отсутствуют.",
                f"{layout}. Эти помещения отсутствуют.",
                False,
            ),
            (
                "Спальня 24 м² (она же комната 24 м²).",
                "Спальня 24 м² (она же комната 24 м²).",
                False,
            ),
            (f"{layout}, кабинет 9 м².", f"{layout}, кабинет 10 м².", False),
        )
        with as_app():
            for index, (first_text, second_text, attach) in enumerate(cases):
                with self.subTest(index=index):
                    # Arrange
                    first = {
                        **card(f"95000900{index}"),
                        "address": f"Самара, улица Гагарина, {100 + index}",
                        "description": first_text,
                    }
                    second = {
                        **first,
                        "source_code": "domclick",
                        "description": second_text,
                    }

                    # Act
                    original = self.accept(first)
                    result = self.accept(second)

                    # Assert
                    self.assertEqual(result.property_id == original.property_id, attach)

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
            "description": "Кухня-гостиная 28 м², спальня 24 м².",
        }
        incoming: ActiveSnapshot = {
            **card("950000011"),
            "source_code": "domclick",
            "address": f"{short_address}, квартира 25",
            "area_m2": Decimal("110"),
            "rooms": 3,
            "floor": 12,
            "floors_total": 20,
            "description": "⭐ КУХНЯ-ГОСТИНАЯ 28 м²,\n\nспальня 24 м².",
        }
        another = {
            **incoming,
            "external_id": "950000012",
            "address": f"{short_address}, квартира 26",
        }
        numberless = {**incoming, "external_id": "950000013", "address": short_address}
        empty = {**numberless, "external_id": "950000014", "description": None}
        repeat = {**later(incoming, 1), "address": short_address, "rooms": None}
        shifted = {**later(incoming, 2), "address": another["address"]}

        with as_app() as connection:
            # Act
            first = self.accept(source)
            with self.assertRaises(IntegrityError):
                self.accept({**incoming, "price": price("12345")})
            rolled_back = connection.execute(
                "SELECT address, rooms FROM properties WHERE id=%s",
                (first.property_id,),
            ).fetchone()
            attached = self.accept(incoming)
            separate = [self.accept(data) for data in (another, numberless, empty)]
            preserved = self.accept(repeat)
            stale = self.accept(
                {**shifted, "observed_at": START - timedelta(minutes=1)}
            )
            conflict = self.accept(shifted)
            ambiguous_repeat = self.accept(
                {**shifted, "address": f"{short_address}, кв. 26, кв. 27", "rooms": 4},
            )
            qualified_address = f"{short_address}, квартира 25/1"
            qualified = {
                **incoming,
                "external_id": "950000015",
                "address": qualified_address,
                "rooms": None,
            }
            qualified_created = self.accept(qualified)
            conflicting_qualifier = self.accept(
                {
                    **qualified,
                    "observed_at": repeat["observed_at"],
                    "address": another["address"],
                    "rooms": 4,
                },
            )
            qualified_facts = connection.execute(
                "SELECT address, rooms FROM properties WHERE id=%s",
                (qualified_created.property_id,),
            ).fetchone()
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
            self.assertEqual(
                (stale.outcome, conflict.outcome, ambiguous_repeat.outcome),
                ("stale", "conflict", "conflict"),
            )
            self.assertEqual(conflicting_qualifier.outcome, "conflict")
            self.assertEqual(qualified_facts, (qualified_address, None))
            self.assertEqual(
                rows, (f"{original_address}, квартира 25", 3, repeat["observed_at"])
            )

    def test_create_repeat_and_price_state_changes(self) -> None:
        from app.catalog.models import Ad

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
                self.accept(snap)
                for snap in (source, unchanged, qualifier, changed, returned)
            ]
            ad = Ad.objects.values_list(
                "id",
                "property_id",
                "observed_at",
                "photos",
                "photos_observed_at",
                "property__area_m2",
            ).get(source_id="cian", external_id=source["external_id"])
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
        from app.catalog.models import Ad

        # Arrange
        source = card("940000002")
        with as_app():
            created = self.accept(source)
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
            invalid_photos = [
                {**later(source, 2), "photos": {"status": "known", "urls": [url]}}
                for url in (
                    "https://cdn.example.test:bad/a.jpg",
                    "https://cdn_bad.example.test/a.jpg",
                )
            ]
            invalid_removal = dict(state(source, "removed", 2))
            del invalid_removal["url"]
            incompatible_removal = {
                **state(source, "removed", 2),
                "deal_type": "month_rent",
            }
            bad_host = invalid_photos[-1]
            unknown_region = {**bad_host, "region_id": 2**62}
            changed_scope = {**bad_host, "property_type_id": 2}
            malformed_gallery = {
                **unknown_region,
                "photos": {"status": "known", "urls": ["not-a-url"]},
            }

            # Act
            malformed_result = self.accept(malformed_gallery)
            results = [
                self.accept(snap)
                for snap in (
                    *receipts_input,
                    invalid,
                    *invalid_text,
                    *invalid_photos,
                    unknown_region,
                    changed_scope,
                    invalid_removal,
                    incompatible_removal,
                    state(source, "unavailable"),
                )
            ]
            assert created.ad_id is not None
            ad = Ad.objects.values_list(
                "deal_type", "observed_at", "is_active", "photos"
            ).get(pk=created.ad_id)

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
                    "rejected",
                    "conflict",
                    "rejected",
                    "conflict",
                    "unavailable",
                ],
            )
            conflict = results[2]
            self.assertEqual(results[7].reason, "invalid_active_snapshot")
            self.assertEqual(results[8].reason, "unknown_reference")
            self.assertEqual(malformed_result.reason, "invalid_active_snapshot")
            self.assertEqual(results[9].reason, "property_scope_changed")
            self.assertEqual(conflict.reason, "deal_type_changed")
            self.assertEqual(
                (conflict.previous_deal_type, conflict.incoming_deal_type),
                ("sale", "month_rent"),
            )
            self.assertEqual(ad, ("sale", START, True, [PHOTO]))

    def test_removed_and_photo_states_preserve_identity_and_history(self) -> None:
        from app.catalog.models import Ad

        # Arrange
        source = card("940000003")
        missing_photo = later(source, 1)
        missing_photo["price"] = {"status": "unavailable"}
        missing_photo["photos"] = {"status": "unavailable"}
        empty_photo = later(missing_photo, 3)
        empty_photo["photos"] = {"status": "known", "urls": []}
        no_gallery = card("940000013")
        no_gallery["photos"] = {"status": "unavailable"}
        with as_app() as connection:
            # Act
            created_without_photos = self.accept(no_gallery)
            assert created_without_photos.ad_id is not None
            initial_gallery = Ad.objects.values_list(
                "photos", "photos_observed_at"
            ).get(pk=created_without_photos.ad_id)
            missing_ad = self.accept(state(source, "removed"))
            created = self.accept(source)
            unchanged = self.accept(missing_photo)
            removed = self.accept(state(source, "removed", 2))
            assert created.ad_id is not None
            middle = Ad.objects.values_list(
                "is_active", "photos", "photos_observed_at"
            ).get(pk=created.ad_id)
            restored = self.accept(empty_photo)
            final = Ad.objects.values_list(
                "is_active", "photos", "photos_observed_at"
            ).get(pk=created.ad_id)
            history = connection.execute(
                "SELECT count(*) FROM price_history WHERE ad_id=%s", (created.ad_id,)
            ).fetchone()

            # Assert
            self.assertEqual(created_without_photos.outcome, "created")
            self.assertEqual(initial_gallery, ([], None))
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

            with self.assertRaises(IntegrityError) as failure:
                # Act
                self.accept(source)

            # Assert
            cause = failure.exception.__cause__
            self.assertIsInstance(cause, psycopg.errors.CheckViolation)
            assert isinstance(cause, psycopg.errors.CheckViolation)
            self.assertEqual(cause.diag.constraint_name, "t15_fail_fixture")
            self.assertEqual(
                connection.execute("SELECT count(*) FROM properties").fetchone(), before
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM ads WHERE source_code='cian' AND external_id=%s",
                    (source["external_id"],),
                ).fetchone()
            )

    def test_concurrent_first_snapshots_on_real_database(self) -> None:
        from app.catalog.models import PriceHistory

        for different_sources in (False, True):
            with self.subTest(different_sources=different_sources):
                # Arrange
                first = card("940000005")
                second = first
                if different_sources:
                    first = {
                        **first,
                        "external_id": "940000016",
                        "address": "Самара, улица Гагарина, 999, кв. 31",
                    }
                    second = {**first, "source_code": "domclick"}
                ready = Barrier(2)

                def submit(
                    snapshot: ActiveSnapshot, barrier: Barrier
                ) -> SnapshotResult:
                    with as_app():
                        barrier.wait(timeout=5)
                        return self.accept(snapshot)

                # Act
                with ThreadPoolExecutor(max_workers=2) as callers:
                    calls = [
                        callers.submit(submit, snapshot, ready)
                        for snapshot in (first, second)
                    ]
                    results = [call.result(timeout=10) for call in calls]

                # Assert
                self.assertEqual(results[0].property_id, results[1].property_id)
                ad_ids = {
                    result.ad_id for result in results if result.ad_id is not None
                }
                outcomes = sorted(result.outcome for result in results)
                self.assertEqual(
                    outcomes,
                    ["created"] * 2 if different_sources else ["created", "ignored"],
                )
                self.assertEqual(len(ad_ids), 2 if different_sources else 1)
                with as_app():
                    self.assertEqual(
                        PriceHistory.objects.filter(ad_id__in=ad_ids).count(),
                        len(ad_ids),
                    )


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.config.settings")
    django.setup()
    main(verbosity=2)
