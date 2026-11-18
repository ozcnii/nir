from django.test import TestCase

from catalog import queries
from catalog.models import Ad, PriceHistory, Property, PropertyType, Region, Source


class ReferenceQueriesTests(TestCase):
    def test_seed_and_canonical_values_survive_label_changes(self) -> None:
        # Arrange: test database prepared by the initial migration.
        sources = dict(Source.objects.values_list("code", "enabled"))

        # Act
        PropertyType.objects.filter(pk=1).update(name="Квартиры")
        Region.objects.filter(pk=68).update(name="Самарский регион")
        Source.objects.filter(pk="domclick").update(enabled=False)
        property_type_count = PropertyType.objects.count()
        region_count = Region.objects.count()
        moscow = Region.objects.get(pk=82).name
        has_properties = Property.objects.exists()
        has_ads = Ad.objects.exists()
        has_price_history = PriceHistory.objects.exists()
        kind = queries.property_kind(1)
        name = queries.region_name(68)
        enabled = queries.source_enabled("domclick")
        partner_source = queries.source_enabled("domclick24")

        # Assert
        self.assertEqual(sources, {"cian": True, "domclick": True, "avito": True})
        self.assertEqual(property_type_count, 3)
        self.assertEqual(region_count, 89)
        self.assertEqual(moscow, "Москва")
        self.assertFalse(has_properties)
        self.assertFalse(has_ads)
        self.assertFalse(has_price_history)
        self.assertEqual(kind, "flat")
        self.assertEqual(name, "Самарская область")
        self.assertIs(enabled, False)
        self.assertIsNone(partner_source)

    def test_new_row_with_same_label_does_not_replace_deleted_standard_id(self) -> None:
        # Arrange
        PropertyType.objects.filter(pk=1).delete()
        Region.objects.filter(pk=68).delete()

        # Act
        property_type = PropertyType.objects.create(name="Квартира")
        region = Region.objects.create(name="Самарская область")
        old_kind = queries.property_kind(1)
        new_kind = queries.property_kind(property_type.id)
        old_region = queries.region_name(68)
        new_region = queries.region_name(region.id)

        # Assert
        self.assertGreaterEqual(property_type.id, 4)
        self.assertGreaterEqual(region.id, 90)
        self.assertIsNone(old_kind)
        self.assertIsNone(new_kind)
        self.assertIsNone(old_region)
        self.assertIsNone(new_region)
