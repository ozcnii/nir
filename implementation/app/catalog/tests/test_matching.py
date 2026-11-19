from unittest import TestCase

from app.catalog.address_parser import parse_address
from app.catalog.contracts import PropertyFacts
from app.catalog.matching_room import repeated_room_patch
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

    def test_street_suffix_uses_the_canonical_street_type(self) -> None:
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
        results = [parse_address(address, region) for address in addresses]
        misplaced_street = parse_address("Солнечная ул., Песочная ул., 7", region)

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
        valid = "Самара, улица 8 Марта, 7"
        incomplete = (
            "Самара, участок 9",
            "Самара, ул. Солнечная, участок 9",
            "Самара, ул. Солнечная участок 9",
            "Самара, ул. Солнечная, дом 7, участок 9",
            "Самара, ул. Солнечная 7, 9",
            "Самара, ул. Солнечная, 7, 9",
        )

        # Act
        complete = parse_address(f"{valid}, {tail}", region, room=True)
        results = [
            parse_address(f"{address}, {tail}", region, room=True)
            for address in incomplete
        ]

        # Assert
        assert complete is not None
        self.assertEqual((complete.street, complete.number), ("улица 8 марта", "7"))
        self.assertEqual(results, [None] * len(incomplete))

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
            (f"{territory}, дом 7, стр. б", True),
            (f"{territory}, дом 7, квартира 25, комната 3", True),
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
