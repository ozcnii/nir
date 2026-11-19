from unittest import TestCase

from app.catalog.address_parser import parse_address
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
