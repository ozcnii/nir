from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.expressions import RawSQL
from django.test import TransactionTestCase
from psycopg import Error

from catalog.models import (
    Ad,
    DealType,
    MarketSegment,
    PriceHistory,
    PriceKind,
    Property,
    PropertyType,
    Region,
    Source,
)

OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class CatalogSchemaTests(TransactionTestCase):
    def setUp(self) -> None:
        self.region = Region.objects.create(name="Тестовый регион")
        self.property_type = PropertyType.objects.create(name="Квартира")
        self.property = Property.objects.create(
            region=self.region, property_type=self.property_type
        )
        self.source = Source.objects.create(code="cian", enabled=True)
        self.ad = Ad.objects.create(
            property=self.property,
            source=self.source,
            external_id="12345",
            url="https://example.org/ad/12345",
            deal_type=DealType.SALE,
            is_active=True,
            observed_at=OBSERVED_AT,
        )

    def test_unknown_values_and_confirmed_empty_gallery(self) -> None:
        self.property.refresh_from_db()
        self.ad.refresh_from_db()

        self.assertIsNone(self.property.address)
        self.assertIsNone(self.property.latitude)
        self.assertIsNone(self.property.longitude)
        self.assertIsNone(self.property.area_m2)
        self.assertIsNone(self.property.rooms)
        self.assertIsNone(self.property.floor)
        self.assertIsNone(self.property.floors_total)
        self.assertIsNone(self.property.land_area_m2)
        self.assertIsNone(self.ad.market_segment)
        self.assertIsNone(self.ad.description)
        self.assertIsNone(self.ad.photos_observed_at)
        self.assertEqual(self.ad.photos, [])
        self.assertFalse(PriceHistory.objects.filter(ad=self.ad).exists())

        self.ad.photos_observed_at = OBSERVED_AT
        self.ad.save(update_fields=["photos_observed_at"])
        self.ad.refresh_from_db()

        self.assertEqual(self.ad.photos_observed_at, OBSERVED_AT)
        self.assertEqual(self.ad.photos, [])

    def test_coordinates_at_boundaries_and_published_zero_rooms(self) -> None:
        Property.objects.filter(pk=self.property.pk).update(
            latitude=Decimal("-90"),
            longitude=Decimal("180"),
            rooms=0,
            floor=-1,
            floors_total=1,
            area_m2=Decimal("0.01"),
            land_area_m2=Decimal("0.01"),
        )
        self.property.refresh_from_db()

        self.assertEqual(self.property.latitude, Decimal("-90.0000000"))
        self.assertEqual(self.property.longitude, Decimal("180.0000000"))
        self.assertEqual(self.property.rooms, 0)
        self.assertEqual(self.property.floor, -1)
        self.assertEqual(self.property.floors_total, 1)
        self.assertEqual(self.property.area_m2, Decimal("0.01"))
        self.assertEqual(self.property.land_area_m2, Decimal("0.01"))

    def test_invalid_physical_measurements(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"latitude": Decimal("0")}, "property_coordinates_pair_in_range"),
            ({"longitude": Decimal("0")}, "property_coordinates_pair_in_range"),
            (
                {"latitude": Decimal("90.0000001"), "longitude": Decimal("0")},
                "property_coordinates_pair_in_range",
            ),
            (
                {"latitude": Decimal("0"), "longitude": Decimal("-180.0000001")},
                "property_coordinates_pair_in_range",
            ),
            ({"area_m2": Decimal("0")}, "property_area_positive"),
            ({"area_m2": RawSQL("'NaN'::numeric", ())}, "property_area_positive"),
            ({"land_area_m2": Decimal("-0.01")}, "property_land_area_positive"),
            ({"rooms": -1}, "property_rooms_nonnegative"),
            ({"floors_total": 0}, "property_floors_total_positive"),
            ({"floor": 2, "floors_total": 1}, "property_floor_within_building"),
        )

        for fields, name in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError) as caught:
                    Property.objects.filter(pk=self.property.pk).update(**fields)
                self.assert_database_error(caught.exception, "23514", name)

    def test_gallery_preserves_full_urls_and_order(self) -> None:
        urls = [
            "https://images.example.org/1.webp?width=512&signature=x#image",
            "http://images.example.org/2.jpg",
            "https://[2001:db8::1]:8443/3.jpg?signature=x#image",
        ]

        Ad.objects.filter(pk=self.ad.pk).update(
            photos=urls,
            photos_observed_at=OBSERVED_AT - timedelta(seconds=1),
            market_segment=MarketSegment.RESALE,
        )
        self.ad.refresh_from_db()

        self.assertEqual(self.ad.photos, urls)
        self.assertEqual(self.ad.market_segment, MarketSegment.RESALE)

    def test_invalid_gallery_shape_and_timestamp(self) -> None:
        url = "https://images.example.org/1.jpg"
        invalid_items: tuple[object, ...] = (
            {"url": url},
            url,
            0,
            [True],
            [None],
            [{}],
            [[url]],
            [url, ""],
            ["not-a-url"],
            ["https:///not-an-authority"],
            ["https://:443/a.jpg"],
            ["https://@/a.jpg"],
            ["https://example.org/with spaces"],
        )
        for value in invalid_items:
            with self.subTest(value=value):
                with self.assertRaises(IntegrityError) as caught:
                    Ad.objects.filter(pk=self.ad.pk).update(
                        photos=value, photos_observed_at=OBSERVED_AT
                    )
                self.assert_database_error(
                    caught.exception, "23514", "ad_photos_url_array"
                )

        with self.assertRaises(IntegrityError) as caught:
            Ad.objects.filter(pk=self.ad.pk).update(photos=[url])
        self.assert_database_error(caught.exception, "23514", "ad_unknown_photos_empty")

        with self.assertRaises(IntegrityError) as caught:
            Ad.objects.filter(pk=self.ad.pk).update(
                photos_observed_at=OBSERVED_AT + timedelta(microseconds=1)
            )
        self.assert_database_error(
            caught.exception, "23514", "ad_photos_observed_in_time"
        )

    def test_price_history_accepts_zero_and_rejects_invalid_values(self) -> None:
        price = PriceHistory.objects.create(
            ad=self.ad,
            observed_at=OBSERVED_AT,
            price_rub=0,
            price_kind=PriceKind.LISTED,
        )
        price.refresh_from_db()
        self.assertEqual(price.price_rub, Decimal("0.00"))

        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"price_rub": Decimal("-0.01")}, "price_history_amount_nonnegative"),
            (
                {"price_rub": RawSQL("'NaN'::numeric", ())},
                "price_history_amount_nonnegative",
            ),
            ({"price_kind": "unknown"}, "price_history_kind_valid"),
        )
        for fields, name in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError) as caught:
                    PriceHistory.objects.filter(pk=price.pk).update(**fields)
                self.assert_database_error(caught.exception, "23514", name)

    def test_key_is_unique_per_source_and_history_per_ad_and_time(self) -> None:
        other = Ad.objects.create(
            property=self.property,
            source=Source.objects.create(code="domclick", enabled=True),
            external_id=self.ad.external_id,
            url="https://example.net/ad/12345",
            deal_type=DealType.MONTH_RENT,
            is_active=True,
            observed_at=OBSERVED_AT,
        )
        with self.assertRaises(IntegrityError) as caught:
            Ad.objects.filter(pk=other.pk).update(source=self.source)
        self.assert_database_error(
            caught.exception, "23505", "ad_source_external_id_unique"
        )

        for ad, observed_at in (
            (self.ad, OBSERVED_AT),
            (self.ad, OBSERVED_AT + timedelta(seconds=1)),
            (other, OBSERVED_AT),
        ):
            PriceHistory.objects.create(
                ad=ad, observed_at=observed_at, price_rub=1, price_kind=PriceKind.FROM
            )
        with self.assertRaises(IntegrityError) as caught:
            PriceHistory.objects.create(
                ad=self.ad,
                observed_at=OBSERVED_AT,
                price_rub=2,
                price_kind=PriceKind.LISTED,
            )
        self.assert_database_error(
            caught.exception, "23505", "price_history_ad_observed_unique"
        )

    def test_invalid_required_text_and_enum_values(self) -> None:
        with self.assertRaises(IntegrityError) as caught:
            Source.objects.create(code=" ", enabled=False)
        self.assert_database_error(caught.exception, "23514", "source_code_present")

        for model, present, unique, existing in (
            (Region, "region_name_present", "region_name_unique", self.region),
            (
                PropertyType,
                "property_type_name_present",
                "property_type_name_unique",
                self.property_type,
            ),
        ):
            with self.subTest(table=model._meta.db_table):
                with self.assertRaises(IntegrityError) as caught:
                    model.objects.create(name="")
                self.assert_database_error(caught.exception, "23514", present)
                with self.assertRaises(IntegrityError) as caught:
                    model.objects.create(name=existing.name)
                self.assert_database_error(caught.exception, "23505", unique)

        for field, name in (
            ("external_id", "ad_external_id_present"),
            ("url", "ad_url_present"),
            ("deal_type", "ad_deal_type_valid"),
            ("market_segment", "ad_market_segment_valid"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError) as caught:
                    Ad.objects.filter(pk=self.ad.pk).update(**{field: ""})
                self.assert_database_error(caught.exception, "23514", name)

    def test_foreign_keys_reject_missing_targets(self) -> None:
        price = PriceHistory.objects.create(
            ad=self.ad,
            observed_at=OBSERVED_AT,
            price_rub=1,
            price_kind=PriceKind.LISTED,
        )
        for row, field, target in (
            (self.property, "region_id", -1),
            (self.property, "property_type_id", -1),
            (self.ad, "property_id", -1),
            (self.ad, "source_id", "unknown-source"),
            (price, "ad_id", -1),
        ):
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError) as caught, transaction.atomic():
                    type(row).objects.filter(pk=row.pk).update(**{field: target})
                self.assert_database_error(caught.exception, "23503")

    def test_used_references_and_history_survive_delete_attempts(self) -> None:
        price = PriceHistory.objects.create(
            ad=self.ad,
            observed_at=OBSERVED_AT,
            price_rub=1,
            price_kind=PriceKind.LISTED,
        )
        for row, key in (
            (self.region, "id"),
            (self.property_type, "id"),
            (self.property, "id"),
            (self.source, "code"),
            (self.ad, "id"),
        ):
            table = row._meta.db_table
            with self.subTest(table=table):
                with self.assertRaises(ProtectedError):
                    row.delete()
                with self.assertRaises(IntegrityError) as caught:
                    with transaction.atomic(), connection.cursor() as cursor:
                        cursor.execute(
                            f"DELETE FROM {table} WHERE {key} = %s", [row.pk]
                        )
                self.assert_database_error(caught.exception, "23503")

        price.refresh_from_db()
        self.assertEqual(price.ad_id, self.ad.pk)

    def assert_database_error(
        self, error: IntegrityError, sqlstate: str, constraint: str | None = None
    ) -> None:
        cause = error.__cause__
        assert isinstance(cause, Error)
        self.assertEqual(cause.sqlstate, sqlstate)
        if constraint is not None:
            self.assertEqual(cause.diag.constraint_name, constraint)
