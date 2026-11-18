from unittest import TestCase

from catalog.matching import _has_layout, parse_address


class MatchingTests(TestCase):
    def test_district_is_not_a_settlement(self) -> None:
        # Arrange
        region = "Самарская область"
        addresses = (
            "Самарская область, Приволжский район, улица Центральная, 15, кв. 25",
            "Самара, Ленинский район, 17, кв. 25",
            "Самара, Советский р-н, 17",
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
        ):
            with self.subTest(text=text):
                # Arrange: two measurements without two asserted rooms.
                # Act
                result = _has_layout(text)

                # Assert
                self.assertFalse(result)

        # Arrange
        text = "кухня-гостиная 28 кв. м. спальня 24 кв. м."

        # Act
        result = _has_layout(text)

        # Assert
        self.assertTrue(result)
