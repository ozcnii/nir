"""Exact URL validation shared by the catalog and its future JSON endpoint."""

from django.http import QueryDict
from django.test import TestCase, TransactionTestCase

from app.catalog.catalog_filters import CatalogFilters


class CatalogFiltersTests(TestCase):
    def test_independent_errors_and_good_filters_are_preserved(self) -> None:
        # Arrange
        query = QueryDict(
            "deal_type=&category=room&category=house&region_id=9223372036854775808"
            "&price_min=NaN&area_min=3,25&area_max=1&rooms_min=1.5"
            "&floor_min=-2&floor_max=0&page_size=20&page="
        )

        # Act
        form = CatalogFilters(query)
        codes = {
            name: [error["code"] for error in errors]
            for name, errors in form.field_errors().items()
        }

        # Assert
        self.assertEqual(
            codes,
            {
                "deal_type": ["invalid_choice"],
                "category": ["repeated_parameter"],
                "region_id": ["out_of_range"],
                "price_min": ["invalid_decimal"],
                "area_min": ["inverted_range"],
                "area_max": ["inverted_range"],
                "rooms_min": ["invalid_integer"],
                "page": ["invalid_integer"],
                "page_size": ["unknown_parameter"],
            },
        )
        self.assertEqual(
            form.canonical_query(page=1),
            "deal_type=sale&category=all&floor_min=-2&floor_max=0&page=1",
        )

    def test_defaults_and_decimal_canonical_form(self) -> None:
        # Arrange
        form = CatalogFilters(
            QueryDict("category=&price_max=0,50&rooms_min=0&page=0001")
        )

        # Act
        errors = form.field_errors()

        # Assert
        self.assertEqual(errors, {})
        self.assertEqual(
            form.canonical_query(page=form.cleaned_data["page"], from_catalog=True),
            "from=catalog&deal_type=sale&category=all&price_max=0.50&rooms_min=0&page=1",
        )


class CatalogFiltersHttpTests(TransactionTestCase):
    """Let each catalog request manage its own database transaction."""

    def test_nonexistent_region_and_oversized_page(self) -> None:
        # Arrange
        query = {"region_id": "9223372036854775807", "page": "9999999999999999999999"}

        # Act
        missing_region = self.client.get("/catalog/", query)
        missing_page = self.client.get(
            "/catalog/", {"page": query["page"], "category": "room"}
        )

        # Assert
        self.assertContains(missing_region, "Регион не найден.", status_code=400)
        self.assertContains(missing_page, "Такой страницы уже нет.", status_code=400)
        self.assertContains(
            missing_page, "deal_type=sale&amp;category=room&amp;page=1", status_code=400
        )
