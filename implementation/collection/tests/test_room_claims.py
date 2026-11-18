from decimal import Decimal
from unittest import TestCase

from collection.normalization import RejectedCard
from collection.room_claims import room_facts


class RoomClaimsTests(TestCase):
    def test_only_published_room_scope_contributes(self) -> None:
        cases = (
            (None, "Две комнаты общей площадью 23,5 м²", None, 2, Decimal("23.50")),
            (None, "Комната 9 м² и комната 10 м² в одной квартире", None, 2, None),
            (None, "Комната 9 м². Вторую можно купить отдельно", None, 1, Decimal("9")),
            (
                1,
                "",
                "Уютная комната 9,2 м² — у соседки, она не входит в продажу",
                1,
                Decimal("9"),
            ),
            (
                1,
                "",
                "Продаётся 1/2 доля, которой по соглашению соответствует комната № 2",
                1,
                Decimal("9"),
            ),
        )
        for count, title, description, expected_count, expected_area in cases:
            with self.subTest(title=title, description=description):
                # Arrange
                area = Decimal("9") if count else None

                # Act
                result = room_facts(count, area, description, title=title)

                # Assert
                self.assertEqual(result, (expected_count, expected_area))

    def test_conflicting_or_unallocated_unit_stops_before_p3(self) -> None:
        cases = (
            (
                "В квартире ремонт. Продаётся 1/2 доля в квартире",
                "unconfirmed_room_unit",
            ),
            (
                "Доля: комната № 2 на плане, продаётся 1/2 квартиры",
                "unconfirmed_room_unit",
            ),
            ("Продаются две комнаты одним лотом.", "conflicting_room_count"),
            ("Продаётся комната 9,2 м², на фото видна кухня.", "conflicting_room_area"),
        )
        for description, reason in cases:
            with self.subTest(description=description):
                # Arrange
                count, area = 1, Decimal("9")

                with self.assertRaisesRegex(RejectedCard, f"^{reason}$"):
                    # Act
                    room_facts(count, area, description)

                # Assert: the exact failure is validated by assertRaisesRegex.
