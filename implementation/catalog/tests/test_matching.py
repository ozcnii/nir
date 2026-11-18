from unittest import TestCase

from catalog.matching import _has_layout, parse_address


class MatchingTests(TestCase):
    def test_district_is_not_a_settlement(self) -> None:
        # Arrange
        region = "Самарская область"
        address = "Самарская область, Приволжский район, улица Центральная, 15, кв. 25"

        # Act
        result = parse_address(address, region)

        # Assert
        self.assertIsNone(result)

    def test_layout_requires_asserted_dimensions_of_distinct_rooms(self) -> None:
        for text in (
            "кухня 28 м² и спальня 24 м² не предусмотрены",
            "жк предлагает несколько планировок: кухня 28 м², спальня 24 м²",
            "кухня 18 м², кухня 20 м²",
        ):
            with self.subTest(text=text):
                # Arrange: two measurements without two asserted rooms.
                # Act
                result = _has_layout(text)

                # Assert
                self.assertFalse(result)

        # Arrange
        text = "кухня-гостиная 28 м². спальня 24 м²."

        # Act
        result = _has_layout(text)

        # Assert
        self.assertTrue(result)
