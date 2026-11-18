from decimal import Decimal
from unittest import TestCase

from collection.normalization import RejectedCard
from collection.room_claims import room_facts


class RoomClaimsTests(TestCase):
    def test_only_published_room_scope_contributes(self) -> None:
        cases = (
            (
                None,
                "Комната № 2 в квартире № 25 площадью 9,2 м²",
                None,
                1,
                Decimal("9.20"),
            ),
            (
                None,
                "Комната № 2 в квартире № 25 площадью 9 м² и ещё одна комната 10 м² одним лотом",
                None,
                2,
                None,
            ),
            (
                None,
                "Комната 9 м² и ещё одна комната 10 м², общей площадью 19 м²",
                None,
                2,
                Decimal("19"),
            ),
            (
                None,
                "Комната 9 м² и ещё одна комната 10 м² продаётся отдельно",
                None,
                1,
                Decimal("9"),
            ),
            (
                1,
                "",
                "Продаётся комната № 2 в квартире № 25. Площадь квартиры 64 м².",
                1,
                Decimal("9"),
            ),
            (
                1,
                "",
                "Продаётся 1/2 доля, которой соответствует комната № 2 в квартире № 25 площадью 9 м².",
                1,
                Decimal("9"),
            ),
            (None, "Комната 9 м² и комната 10 м², санузел отдельно", None, 2, None),
            (None, "Комната № 2 в квартире № 25", None, 1, None),
            (
                1,
                "",
                "Продаётся комната 9 м². Долю в соседней квартире можно купить отдельно.",
                1,
                Decimal("9"),
            ),
            (1, "", "Продаются две комнаты в соседней квартире.", 1, Decimal("9")),
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

    def test_explicit_room_scope_survives_punctuation(self) -> None:
        for separator in (" и ", ", ", ", и "):
            with self.subTest(separator=separator):
                # Arrange
                title = (
                    f"Комната 9 м²{separator}комната 10 м² и комната 11 м² одним лотом"
                )

                # Act
                result = room_facts(None, None, None, title=title)

                # Assert
                self.assertEqual(result, (3, None))

        for pause in (" ", " — ", " – ", ", "):
            with self.subTest(pause=pause):
                # Arrange
                offer = "Продаётся комната № 2 в квартире № 25"
                share = (
                    f"Доля: комната № 2{pause}на фото, продаётся только 1/2 квартиры"
                )

                # Act
                result = room_facts(1, Decimal("9.2"), f"{offer}{pause}площадью 9,2 м²")
                with self.assertRaisesRegex(RejectedCard, "^unconfirmed_room_unit$"):
                    room_facts(1, Decimal("9"), share)
                with self.assertRaisesRegex(RejectedCard, "^conflicting_room_area$"):
                    room_facts(1, Decimal("9"), f"{offer}{pause}площадью 9,2 м²")

                # Assert
                self.assertEqual(result, (1, Decimal("9.20")))

    def test_published_room_list_is_checked_before_single_room_evidence(self) -> None:
        cases = (
            ("Продаётся комната № 2 и комнаты № 3 и № 4 одним лотом.", (3, None)),
            ("Продаю отдельную комнату в квартире № 25.", (1, None)),
            (
                "Продаётся доля в квартире. По соглашению о пользовании закреплена комната № 2.",
                (1, None),
            ),
            ("Продаются комнаты № 2, № 3 и № 4.", (3, None)),
            ("Продаются 2 комнаты площадью 20 м² одним лотом.", (2, Decimal("20"))),
            ("Продаются комнаты № 2 и № 3 площадью 9 м².", (2, None)),
            (
                "Продаётся комната № 2 площадью 9 м² и ещё одна комната № 3, "
                "которую можно купить отдельно.",
                (1, Decimal("9")),
            ),
            ("Площадь продаваемой комнаты № 2 — 9,2 м².", (None, Decimal("9.20"))),
        )
        for description, expected in cases:
            with self.subTest(description=description):
                # Arrange
                rooms = None

                # Act
                result = room_facts(rooms, None, description)

                # Assert
                self.assertEqual(result, expected)

    def test_area_and_share_stay_with_the_offered_room(self) -> None:
        # Arrange
        description = (
            "Продаётся комната № 2 площадью 9,0 м². "
            "Площадь продаваемой комнаты № 3: 9,2 м². "
            "Комната № 3 продаётся отдельно."
        )
        valid_share = (
            "Продаётся доля в квартире. По соглашению о пользовании "
            "закреплена комната № 2 за этой долей."
        )

        # Act
        measured = room_facts(1, Decimal("9"), description)
        allocated = room_facts(1, Decimal("9"), valid_share)

        # Assert
        self.assertEqual(measured, (1, Decimal("9.00")))
        self.assertEqual(allocated, (1, Decimal("9.00")))

    def test_conflicting_or_unallocated_unit_stops_before_p3(self) -> None:
        cases = (
            (
                "Продаётся 1/2 доля в праве на квартиру. "
                "По соглашению о пользовании закреплена комната № 2 за другим собственником.",
                "unconfirmed_room_unit",
            ),
            (
                "Доля, которой по соглашению соответствует комната № 2. "
                "За этой долей закреплена комната № 3.",
                "unconfirmed_room_unit",
            ),
            (
                "Продаётся комната № 2 площадью 9 м². "
                "Площадь продаваемой комнаты № 2: 9,2 м². "
                "Комната № 3 продаётся отдельно.",
                "conflicting_room_area",
            ),
            (
                "Доля: комната № 2 (на фото), продаётся только 1/2 квартиры.",
                "unconfirmed_room_unit",
            ),
            (
                "Продаются две комнаты: комната № 2, комната № 3 и комната № 4.",
                "conflicting_room_count",
            ),
            (
                "Доля: комната № 2 на втором фото, продаётся только 1/2 квартиры.",
                "unconfirmed_room_unit",
            ),
            (
                "Продаются две комнаты — комната № 2, комната № 3 и комната № 4.",
                "conflicting_room_count",
            ),
            (
                "Две комнаты: комната № 2 и комната № 2 в той же квартире.",
                "conflicting_room_count",
            ),
            ("Площадь продаваемой комнаты № 2: 9,2 м².", "conflicting_room_area"),
            (
                "Продаётся одна комната: комната № 2 площадью 9,2 м².",
                "conflicting_room_area",
            ),
            ("Площадь продаваемой комнаты № 2 — 9,2 м².", "conflicting_room_area"),
            ("Продаётся комната № 2 (площадью 9,2 м²).", "conflicting_room_area"),
            (
                "Доля: комната № 2 на фото, продаётся только 1/2 квартиры.",
                "unconfirmed_room_unit",
            ),
            ("Доля: комната № 2 в квартире № 25 на схеме.", "unconfirmed_room_unit"),
            (
                "Продаётся комната 9 м² и ещё одна комната 10 м² одним лотом.",
                "conflicting_room_count",
            ),
            (
                "Продаётся комната № 2 в квартире № 25 площадью 9,2 м².",
                "conflicting_room_area",
            ),
            ("Продаём комнату площадью 9,2 м².", "conflicting_room_area"),
            ("Арт. 537953. Уютная комната 9,2 м².", "conflicting_room_area"),
            (
                "Продаётся 1/2 доля, которой по соглашению соответствует комната № 2 площадью 9,2 м²",
                "conflicting_room_area",
            ),
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

    def test_share_allocation_requires_an_affirmative_link(self) -> None:
        for claim in (
            "По соглашению закреплена комната № 2 не за этой долей, а за долей соседа.",
            "По соглашению не закреплена комната № 2 за этой долей.",
            "Этой доле не соответствует комната № 2.",
        ):
            with self.subTest(claim=claim):
                # Arrange
                description = (
                    "Продаётся доля, которой соответствует комната № 2. " + claim
                )

                # Act
                with self.assertRaisesRegex(RejectedCard, "^unconfirmed_room_unit$"):
                    room_facts(1, Decimal("9"), description)

                # Assert: the rejection reason is checked above.

        # Arrange
        description = (
            "Продаётся доля в квартире. По соглашению закреплена комната № 2 "
            "за этой долей, ремонт не требуется."
        )

        # Act
        result = room_facts(1, Decimal("9"), description)

        # Assert
        self.assertEqual(result, (1, Decimal("9")))

    def test_separate_sale_keeps_the_apartment_with_its_room(self) -> None:
        for separate in (
            "Комната № 2 в квартире № 26 продаётся отдельно.",
            "Комната № 2 продаётся в квартире № 26 отдельно.",
            "Комната № 2 продаётся отдельно в квартире № 26.",
            "Комната № 2 продаётся отдельно.",
            "Комната № 2 продаётся отдельно в квартире № 25.",
        ):
            with self.subTest(separate=separate):
                # Arrange
                description = (
                    "Продаётся комната № 2 в квартире № 25. "
                    "Площадь продаваемой комнаты № 2 в квартире № 25: 9,2 м². "
                    + separate
                )

                # Act
                with self.assertRaisesRegex(RejectedCard, "^conflicting_room_area$"):
                    room_facts(1, Decimal("9"), description)

                # Assert: the rejection reason is checked above.

        # Arrange
        description = (
            "Площадь продаваемой комнаты № 2 в квартире № 26: 9,2 м². "
            "Комната № 2 продаётся отдельно в квартире № 26."
        )

        # Act
        result = room_facts(1, Decimal("9"), description)

        # Assert
        self.assertEqual(result, (1, Decimal("9")))
