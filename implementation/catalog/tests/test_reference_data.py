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
        kind = queries.property_kind(1)
        name = queries.region_name(68)
        enabled = queries.source_enabled("domclick")

        # Assert
        self.assertEqual(sources, {"cian": True, "domclick": True, "avito": True})
        self.assertEqual(PropertyType.objects.count(), 3)
        self.assertEqual(Region.objects.count(), 89)
        self.assertEqual(Region.objects.get(pk=82).name, "Москва")
        self.assertFalse(Property.objects.exists())
        self.assertFalse(Ad.objects.exists())
        self.assertFalse(PriceHistory.objects.exists())
        self.assertEqual(kind, "flat")
        self.assertEqual(name, "Самарская область")
        self.assertIs(enabled, False)
        self.assertIsNone(queries.source_enabled("domclick24"))

    def test_new_row_with_same_label_does_not_replace_deleted_standard_id(self) -> None:
        # Arrange
        PropertyType.objects.filter(pk=1).delete()
        Region.objects.filter(pk=68).delete()

        # Act
        property_type = PropertyType.objects.create(name="Квартира")
        region = Region.objects.create(name="Самарская область")

        # Assert
        assert property_type.pk is not None
        assert region.pk is not None
        self.assertGreaterEqual(property_type.pk, 4)
        self.assertGreaterEqual(region.pk, 90)
        self.assertIsNone(queries.property_kind(1))
        self.assertIsNone(queries.property_kind(property_type.pk))
        self.assertIsNone(queries.region_name(68))
        self.assertIsNone(queries.region_name(region.pk))
