from django.test import TestCase

from app.catalog.models import Ad, PriceHistory, Property, PropertyType, Region, Source
from app.catalog.references import source_enabled


class ReferenceQueriesTests(TestCase):
    def test_seed_and_source_switch(self) -> None:
        # Arrange: test database prepared by the initial migration.
        sources = dict(Source.objects.values_list("code", "enabled"))

        # Act
        Source.objects.filter(pk="domclick").update(enabled=False)
        property_type_count = PropertyType.objects.count()
        region_count = Region.objects.count()
        moscow = Region.objects.get(pk=82).name
        has_properties = Property.objects.exists()
        has_ads = Ad.objects.exists()
        has_price_history = PriceHistory.objects.exists()
        enabled = source_enabled("domclick")
        partner_source = source_enabled("domclick24")

        # Assert
        self.assertEqual(sources, {"cian": True, "domclick": True, "avito": True})
        self.assertEqual(property_type_count, 3)
        self.assertEqual(region_count, 89)
        self.assertEqual(moscow, "Москва")
        self.assertFalse(has_properties)
        self.assertFalse(has_ads)
        self.assertFalse(has_price_history)
        self.assertIs(enabled, False)
        self.assertIsNone(partner_source)

    def test_new_row_with_same_label_does_not_replace_deleted_standard_id(self) -> None:
        # Arrange
        PropertyType.objects.filter(pk=1).delete()
        Region.objects.filter(pk=68).delete()

        # Act
        property_type = PropertyType.objects.create(name="Квартира")
        region = Region.objects.create(name="Самарская область")
        # Assert
        self.assertGreaterEqual(property_type.id, 4)
        self.assertGreaterEqual(region.id, 90)
        self.assertFalse(PropertyType.objects.filter(pk=1).exists())
        self.assertFalse(Region.objects.filter(pk=68).exists())
