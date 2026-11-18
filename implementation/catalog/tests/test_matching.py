from unittest import TestCase

from catalog.matching import _has_layout, _seller
from catalog.matching_address import parse_address


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
            "Кухня 28 м², спальня 24 м², это размеры соседней квартиры.",
            "Не кухня 28 м², а спальня 24 м².",
        ):
            with self.subTest(text=text):
                # Arrange: two measurements without two asserted rooms.
                # Act
                result = _has_layout(_seller(text))

                # Assert
                self.assertFalse(result)

        # Arrange
        texts = (
            "кухня-гостиная 28 кв. м. спальня 24 кв. м.",
            "кухня 28 м²; спальня 24 м²; без комиссии.",
            "Кухня—28 м²; спальня—24 м².",
            "кухня 28 м², спальня 24 м², без комиссии.",
            "кухня 28 м² спальня 24 м² без комиссии.",
            "Кухня 28 м², спальня 24 м², возможна ипотека.",
            "Кухня 28 м², спальня 24 м², не предусмотрена перепланировка.",
            "Кухня 28 м², спальня 24 м²,\nне предусмотрена перепланировка.",
            "Кухня\n28 м²; спальня 24 м².",
            "Фото соседней квартиры\nВ предлагаемой квартире кухня 16 м² и спальня 19 м².",
            "кухня 28 м². спальня 24 м². фото соседней квартиры.",
            "на фото соседняя квартира. кухня 28 м², спальня 24 м². "
            "в предлагаемой квартире кухня 16 м², спальня 19 м².",
        )

        # Act
        results = [_has_layout(_seller(text)) for text in texts]

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
