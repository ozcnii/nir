from unittest import TestCase

from app.catalog.address_parser import parse_address
from app.catalog.contracts import PropertyFacts
from app.catalog.matching_room import repeated_room_patch
from app.catalog.room_address_parser import parse_room_address
from app.catalog.seller_description import matching_description


class MatchingTests(TestCase):
    def test_district_is_not_a_settlement(self) -> None:
        # Arrange
        region = "Самарская область"
        addresses = (
            "Самарская область, Приволжский район, улица Центральная, 15, кв. 25",
            "Самара, Ленинский район, 17, кв. 25",
            "Самарская область, Кировский р-н, ул. Садовая, 17, кв. 25",
            "Самара, Советский р-н, 17",
            "Самарская область, жилой комплекс, ул. Садовая, 17, кв. 25",
            "Самарская область, Кировский р-н., ул. Садовая, 17, кв. 25",
            "Самара, ул. Гагарина, ул. Садовая, 17, кв. 25",
            "Самарская область, городской округ Самара, улица Садовая, 17",
            "Самарская область, Приволжский р-н. Самарской области, ул. Садовая, 17",
            "Самарская область, территория СНТ Рассвет, ул. Садовая, 17",
            "Самарская область, Самара, жилой комплекс Садовый, 17, кв. 25",
        )

        # Act
        results = [parse_address(address, region) for address in addresses]

        # Assert
        self.assertEqual(results, [None] * len(addresses))

    def test_apartment_preserves_explicit_qualifiers(self) -> None:
        # Arrange
        address = "Самара, улица Гагарина, 17, кв. 25/1"

        # Act
        result = parse_address(address, "Самарская область")

        # Assert
        assert result is not None
        self.assertEqual(result.detail("квартира"), "25/1")

    def test_apartment_retains_its_street_and_house_number_rules(self) -> None:
        # Arrange
        region = "Самарская область"
        suffix = "Самара, Победы ул., дом 7, кв. 25"
        marked_number = "Самара, улица Победы, дом № 7, кв. 25"

        # Act
        abbreviated = parse_address(suffix, region)
        numbered = parse_address(marked_number, region)

        # Assert
        assert abbreviated is not None
        self.assertEqual(abbreviated.street, "победы ул.")
        self.assertIsNone(numbered)

    def test_room_street_suffix_uses_the_canonical_street_type(self) -> None:
        # Arrange
        region = "Самарская область"
        addresses = (
            "Самара, улица Солнечная, 7",
            "Самара, Солнечная улица, 7",
            "Самара, Солнечная ул., 7",
            "Самара, Солнечная ул. 7",
            "Самара, ул. Солнечная, 7",
        )

        # Act
        results = [parse_room_address(address, region) for address in addresses]
        misplaced_street = parse_room_address("Солнечная ул., Песочная ул., 7", region)

        # Assert
        self.assertEqual(
            [result.street for result in results if result is not None],
            ["улица солнечная"] * len(addresses),
        )
        self.assertIsNone(misplaced_street)

    def test_full_room_address_requires_a_unique_building(self) -> None:
        # Arrange
        region = "Самарская область"
        tail = "квартира 25, комната 2"
        valid = ("Самара, улица 8 Марта, 7", "Самара, улица 8 Марта дом №7")
        incomplete = (
            "Самара, участок 9",
            "Самара, ул. Солнечная, участок 9",
            "Самара, ул. Солнечная участок 9",
            "Самара, ул. Солнечная, дом 7, участок 9",
            "Самара, ул. Солнечная 7, 9",
            "Самара, ул. Солнечная, 7, 9",
            "Самара, ул. Солнечная дом 7 дом 9",
            "Самара, ул. Солнечная дом №7;д. № 9",
            "Самара, ул. Солнечная дом №7, д. № 9",
        )

        # Act
        complete = [
            parse_room_address(f"{address}, {tail}", region) for address in valid
        ]
        results = [
            parse_room_address(f"{address}, {tail}", region) for address in incomplete
        ]
        streets = [
            parse_room_address(f"г. Самара, {street}, дом 7, {tail}", region)
            for street in ("улица Самара", "Советская")
        ]
        repeated_number = parse_room_address(f"{valid[0]}, {tail}, дом 7", region)
        redundant_chain = [
            parse_room_address(f"{valid[0]}, {tail}, {label}", region)
            for label in ("кв. 25", "комн. 2")
        ]

        # Assert
        self.assertEqual(
            [
                (address.street, address.number)
                for address in complete
                if address is not None
            ],
            [("улица 8 марта", "7")] * len(valid),
        )
        self.assertEqual(results, [None] * len(incomplete))
        self.assertEqual(
            [address.street for address in streets if address is not None],
            ["улица самара", "советская"],
        )
        self.assertIsNone(repeated_number)
        self.assertEqual(redundant_chain, [None, None])

    def test_room_repeat_compares_only_known_fields_in_incomplete_addresses(
        self,
    ) -> None:
        # Arrange
        region = "Самарская область"
        territory = "Самара, микрорайон Солнечный, улица Солнечная"
        known = "дом 7, корпус 2, строение а, квартира 25, комната 2"
        previous: PropertyFacts = {
            "address": f"Самара, улица Солнечная, {known}",
            "latitude": None,
            "longitude": None,
            "area_m2": None,
            "rooms": 1,
            "floor": None,
            "floors_total": None,
            "land_area_m2": None,
        }
        cases = (
            (f"{territory}, дом 7, кв. 25, комн. № 2", False),
            (f"{territory}, д. 7, квартира 26", True),
            (f"{territory}, дом 9, квартира 25", True),
            (f"{territory} д. 9, квартира 25", True),
            (f"{territory}, дом 7, корп. 4", True),
            (f"{territory}, дом 7, корпус 4, корп. 4", True),
            (f"{territory}, дом 7, стр. б", True),
            (f"{territory}, дом 7, квартира 25, комната 3", True),
            (f"{territory}, дом 7, квартира 25, комната 3, комн. 3", True),
            (f"{territory}, дом 7, квартира 26, кв. 26, комната 2", True),
            (f"{territory}, дом 7, кв. 25,, комната 3, комн. 3", False),
            (f"{territory}, дом 7, комната дома 2", True),
            (f"{territory}, дом 7, квартира 25", False),
            (f"{territory}, дом 7, комната 3", False),
            (f"{territory}, дом 7, квартира 25, комната дома 3", False),
            (f"{territory}, 9, квартира 25", True),
            (f"{territory} 9, квартира 25", True),
            (f"{territory}, дом № 9, квартира 25", True),
            (f"{territory} д. №9, квартира 25", True),
            (f"{territory} д. №7, квартира 25", False),
            (f"{territory}, дом № 7, квартира 25", False),
            (f"{territory}, дом № 7, д. №9, квартира 25", False),
            (f"{territory}, дом № 7 или № 9, квартира 25", False),
            ("Самара, улица Солнечная, микрорайон Солнечный, 9, квартира 25", False),
            ("Самара, ул. Солнечная 7, микрорайон Солнечный, квартира 25", False),
            ("Самара, ул. Солнечная 9, микрорайон Солнечный, квартира 25", True),
            ("Самара, ул. Солнечная, дом № 9, микрорайон Солнечный, кв. 25", True),
            ("г. Самара, микрорайон Солнечный, Солнечная ул., 7, кв. 25", False),
            ("г. Новокуйбышевск, микрорайон Солнечный, ул. Солнечная, 7", True),
            ("Новокуйбышевск, микрорайон Солнечный, ул. Солнечная, 7", True),
            ("г. Самара, г. Чапаевск, микрорайон Солнечный, ул. Солнечная, 7", False),
            ("Самара, микрорайон Солнечный, ул. Полевая, 7", True),
            ("Самара, микрорайон Солнечный, ул. Полевая", True),
            ("Самара, микрорайон Солнечный, ул. Солнечная, ул. Полевая, 9", False),
            (f"{territory}, участок 9, квартира 25", False),
            (f"{territory}, дом 7, дом 9, квартира 25", False),
            (f"{territory}, дом 7, квартира 25, комната 3, квартира 26", False),
            (f"{territory}, дом 7, квартира 25, другое здание, комната 3", False),
            ("Самара, ул. Солнечная 7, 9, квартира 25", False),
            ("Самара, ул. Солнечная, 7, 9, квартира 25", False),
            ("Самара, участок 9, квартира 25, комната 2", False),
            ("Самара, ул. Полевая, 7, квартира 25, квартира 26", True),
            (f"{territory}, дом 9, квартира 25, квартира 26", True),
            (f"{territory}, дом 9, кв25?, комната 2", True),
            (f"{territory}, дом 9, квартира 25,, комната 2", True),
            (f"{territory}, дом 7, кв25?, комната 3", False),
            (f"{territory}, дом 7, корпус 3, квартира 25, квартира 26", True),
            (f"{territory}, дом 7, строение б, кв25?, комната 2", True),
            (f"{territory}, дом 7, квартира 26, комната 2, комната 3", True),
            (f"{territory}, дом 7, квартира 25, корпус ?, комната 3", False),
            (f"{territory}, дом 7, квартира 25, комната 3, корпус ?", True),
            (f"{territory}, дом 7, корпус 2, корпус 3, квартира 25, комната 2", False),
            (f"{territory}, дом 7, корпус 2, корпус 3, квартира 25, комната 3", True),
            (f"{territory} дом 7 дом 9, квартира 26", False),
            ("Самара, улица Полевая дом 7 дом 9, квартира 25", True),
            ("Самара, улица Солнечная дом №7;д. №9, квартира 26", False),
            (f"{territory}, дом 9, квартира 25, комната 2, дом 7", False),
            (f"{territory}, дом 9, квартира 25, дом 7, комната 2", False),
            (f"{territory}, дом 9, квартира 25, комната 2, дом 9", True),
            (f"{territory}, дом 9, дом 9, квартира 25, комната 2", True),
            (f"{territory} дом 9 дом 9, квартира 25, комната 2", True),
            (
                "Самара, Полевая дом 7 и Солнечная дом 7, квартира 25, комната 2",
                False,
            ),
            ("Самара, ул. Полевая, Полевая улица, дом 7, кв. 25", True),
            (f"{territory}, 9, квартира 25, комната 2, дом 9", True),
            (f"{territory}, дом 7, квартира 25, комната 2, дом 7", False),
            (f"{territory}, дом 9, квартира 25, участок 7", True),
        )

        for address, conflict in cases:
            with self.subTest(address=address):
                # Arrange
                incoming: PropertyFacts = {**previous, "address": address}

                # Act
                strict_saved = repeated_room_patch(previous, incoming, region)
                partial_saved = repeated_room_patch(
                    {**previous, "address": f"{territory}, {known}"}, incoming, region
                )

                # Assert
                expected: dict[str, object] | None = None if conflict else {}
                self.assertEqual((strict_saved, partial_saved), (expected, expected))

    def test_room_repeat_preserves_known_districts_with_incomplete_unit(self) -> None:
        region = "Самарская область"
        locality = "село Красный Яр, Советский район, улица Советская, дом 7"
        saved_address = f"Красноярский район, {locality}, квартира 25, комната 2"
        untyped_street = saved_address.replace("Советский район, улица ", "")
        two_districts_untyped_street = saved_address.replace(
            "улица Советская", "Советская"
        )
        previous: PropertyFacts = {
            "address": saved_address,
            "latitude": None,
            "longitude": None,
            "area_m2": None,
            "rooms": 1,
            "floor": None,
            "floors_total": None,
            "land_area_m2": None,
        }
        cases = (
            (saved_address, saved_address.replace("Красноярский", "Приволжский"), True),
            (saved_address, saved_address.replace("Советский", "Кировский"), True),
            (
                saved_address,
                saved_address.replace(
                    "Красноярский район", "Приволжский район, Приволжский р-н"
                ),
                True,
            ),
            (
                saved_address,
                saved_address.replace(
                    "Красноярский район", "Приволжский район, Красноярский р-н"
                ),
                False,
            ),
            (saved_address, saved_address.replace("Красноярский район, ", ""), False),
            (saved_address, saved_address.replace("Советский район, ", ""), False),
            (
                saved_address,
                saved_address.replace("Красноярский район", "район Красноярский"),
                False,
            ),
            (
                untyped_street,
                untyped_street.replace("Красноярский", "Приволжский"),
                True,
            ),
            (
                untyped_street,
                two_districts_untyped_street.replace("Красноярский", "Приволжский"),
                True,
            ),
            (untyped_street, two_districts_untyped_street, False),
            (
                two_districts_untyped_street.replace("Красноярский район, ", ""),
                two_districts_untyped_street.replace("Советская", "Кировская"),
                True,
            ),
            (saved_address, two_districts_untyped_street, False),
            (
                saved_address,
                two_districts_untyped_street.replace("Советская", "переулок Советская"),
                True,
            ),
            (
                saved_address,
                two_districts_untyped_street.replace("Советская", "Кировская, Полевая"),
                False,
            ),
        )

        for old, address, conflict in cases:
            with self.subTest(address=address):
                # Arrange
                incomplete_address = address.replace("комната 2", "комната ?")

                # Act
                result = repeated_room_patch(
                    {**previous, "address": old},
                    {**previous, "address": incomplete_address},
                    region,
                )

                # Assert
                self.assertEqual(result, None if conflict else {})

    def test_room_repeat_compares_independently_established_address_facts(self) -> None:
        # Arrange
        region = "Самарская область"
        unit = "Советская, дом 7, квартира 25, комната 2"
        standalone = "г. Самара, улица Советская, дом 7, комната дома 2"
        mixed_scope = standalone + ", квартира 25, комната 2"
        cases = (
            (f"г. Новокуйбышевск, {unit}", f"Самара, г. Самара, {unit}", True),
            (f"г. Новокуйбышевск, {unit}", f"г. Самара, Самара, {unit}", True),
            (f"г. Самара, {unit}", f"Самара, г. Самара, {unit}", False),
            (
                f"г. Самара, {unit}",
                "г. Самара, Самара, дом 7, квартира 25, комната 2",
                False,
            ),
            (
                f"г. Самара, {unit}",
                "Самара, г. Самара, дом 7, квартира 25, комната 2",
                False,
            ),
            (f"г. Самара, {unit}", f"Самара, село Самара, {unit}", True),
            (
                f"г. Самара, {unit}",
                f"город Самара, село Самара, {unit}",
                False,
            ),
            (
                f"г. Новокуйбышевск, улица {unit}",
                f"г. Самара, Тольятти, улица {unit}",
                False,
            ),
            (
                f"г. Новокуйбышевск, улица {unit}",
                f"Тольятти, г. Самара, улица {unit}",
                False,
            ),
            (
                f"г. Новокуйбышевск, {unit}",
                f"Самара, г. Самара, Советский район, {unit}".replace(
                    "комната 2", "комната ?"
                ),
                True,
            ),
            (standalone, mixed_scope, False),
            (standalone, mixed_scope.replace("Самара", "Новокуйбышевск"), True),
        )
        previous: PropertyFacts = {
            "address": None,
            "latitude": None,
            "longitude": None,
            "area_m2": None,
            "rooms": 1,
            "floor": None,
            "floors_total": None,
            "land_area_m2": None,
        }

        for old, incoming, conflict in cases:
            with self.subTest(incoming=incoming):
                # Act
                patch = repeated_room_patch(
                    {**previous, "address": old},
                    {**previous, "address": incoming},
                    region,
                )

                # Assert
                self.assertEqual(patch, None if conflict else {})
                self.assertIsNone(parse_room_address(incoming, region))

    def test_layout_requires_asserted_dimensions_of_distinct_rooms(self) -> None:
        for text in (
            "кухня 28 м² и спальня 24 м² не предусмотрены",
            "жк предлагает несколько планировок: кухня 28 м², спальня 24 м²",
            "кухня 18 м², кухня 20 м²",
            "в соседней квартире кухня 28 м², спальня 24 м².",
            "на фото соседняя квартира: кухня 28 м²; спальня 24 м²; гостиная 31 м².",
            "на фото соседняя квартира. кухня 28 м²; спальня 24 м²; гостиная 31 м².",
            "на фото соседняя квартира. в этой квартире кухня 28 м², спальня 24 м².",
            "кухня 28 м² в предлагаемой квартире, у соседей спальня 24 м².",
            "планировка соседей: кухня 28 м², спальня 24 м².",
            "кухня-гостиная 28 м². кухня 28 м².",
            "в доме общая площадь 80 м². кухня 28 м².",
            "Кухня 28 м² и спальня 24 м² отсутствуют.",
            "В квартире нет таких помещений: кухня 28 м² и спальня 24 м².",
            "Кухня 28 м², спальня 24 м², не предусмотрены.",
            "Кухня -28 м², спальня 24 м².",
            "Кухня 28 м², спальня 24 м², обе отсутствуют.",
            "Кухня 28 м², спальня 24 м². Эти помещения отсутствуют.",
            "Кухня 28 м², спальня 24 м². В предлагаемой квартире эти помещения отсутствуют.",
            "Кухня 28 м², спальня 24 м². Эти размеры не относятся к этой квартире.",
            "Кухня 28 м², спальня 24 м². Эти размеры относятся к соседней квартире.",
            "Кухня-гостиная №1 28 м², кухня №1 28 м².",
            "Кухня 28 м² и спальня 24 м² не относятся к этой квартире.",
            "Спальня 24 м² (она же комната 24 м²).",
            "Спальня 24 м²; комната 24 м².",
            "Спальня №1 24 м²; комната №1 24 м².",
            "Спальня 24 м²; детская 24 м².",
            "Ванная 4 м²; санузел 4 м².",
            "Спальня №1 24 м², спальня №1 23 м².",
            "Спальня 24 м², спальня №1 24 м².",
            "Кухня 28 м², спальня 24 м², это размеры соседней квартиры.",
            "Не кухня 28 м², а спальня 24 м².",
            "Кухня 28 м². Спальня 24 м². Кухня и спальня отсутствуют.",
            "Кухня 28 м², спальня 24 м²?",
            "Кухня 28 м², спальня 24 м². Эти сведения не относятся к предлагаемой квартире.",
            "Кухня 28 м²; спальня 24 м²; без комиссии.",
            "Кухня 28 м², спальня 24 м². Эти помещения не требуют ремонта.",
        ):
            with self.subTest(text=text):
                # Arrange: two measurements without two asserted rooms.
                # Act
                result = matching_description(text)

                # Assert
                self.assertIsNone(result)

        # Arrange
        texts = (
            "кухня-гостиная 28 кв. м. спальня 24 кв. м.",
            "кухня 28 м²; спальня 24 м².",
            "Кухня—28 м²; спальня—24 м².",
            "Спальня № 1 24 м² и спальня № 2 23 м².",
            "Комната № 1 24 м² и комната № 2 23 м².",
            "В квартире кухня 28 м² и спальня 24 м².",
            "В продаваемой квартире: кухня 12 м², спальня 14 м².",
            "Планировка: кухня 28 м², спальня 24 м².",
            "Кухня\n28 м²; спальня 24 м².",
            "⭐ Кухня 28,5 м², спальня 24 м².",
        )

        # Act
        results = [matching_description(text) is not None for text in texts]

        # Assert
        self.assertEqual(results, [True] * len(texts))

    def test_district_can_qualify_a_settlement_but_keeps_its_scope(self) -> None:
        # Arrange
        region = "Самарская область"
        street = "ул. Садовая, 17, кв. 25"

        # Act
        city = parse_address(f"{region}, Самара, Кировский р–н., {street}", region)
        village = parse_address(
            f"{region}, Кировский р-н., с. Лесное, {street}", region
        )

        # Assert
        assert city is not None and village is not None
        self.assertEqual(city.locality, ("самара",))
        self.assertEqual(city.district, "кировский")
        self.assertEqual(village.locality, ("район кировский", "лесное"))
        self.assertIsNone(village.district)
        self.assertEqual(village.settlement_kind, "село")
