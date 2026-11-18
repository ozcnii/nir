from decimal import Decimal

from django.db import models
from django.db.models import F, Func, Q, Value


class Source(models.Model):
    code = models.TextField(primary_key=True)
    enabled = models.BooleanField()

    class Meta:
        db_table = "sources"
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=r"\S"), name="source_code_present"
            ),
        ]


class Region(models.Model):
    name = models.TextField()

    class Meta:
        db_table = "regions"
        constraints = [
            models.UniqueConstraint(fields=["name"], name="region_name_unique"),
            models.CheckConstraint(
                condition=Q(name__regex=r"\S"), name="region_name_present"
            ),
        ]


class PropertyType(models.Model):
    name = models.TextField()

    class Meta:
        db_table = "property_types"
        constraints = [
            models.UniqueConstraint(fields=["name"], name="property_type_name_unique"),
            models.CheckConstraint(
                condition=Q(name__regex=r"\S"), name="property_type_name_present"
            ),
        ]


class Property(models.Model):
    region = models.ForeignKey(Region, on_delete=models.PROTECT)
    property_type = models.ForeignKey(PropertyType, on_delete=models.PROTECT)
    address = models.TextField(null=True, blank=True)
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    area_m2 = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    rooms = models.SmallIntegerField(null=True, blank=True)
    floor = models.SmallIntegerField(null=True, blank=True)
    floors_total = models.SmallIntegerField(null=True, blank=True)
    land_area_m2 = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = "properties"
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(
                    latitude__isnull=False,
                    longitude__isnull=False,
                    latitude__range=(-90, 90),
                    longitude__range=(-180, 180),
                ),
                name="property_coordinates_pair_in_range",
            ),
            models.CheckConstraint(
                # PostgreSQL orders numeric NaN above finite numbers.
                condition=Q(area_m2__isnull=True)
                | Q(area_m2__gt=0, area_m2__lt=Decimal("10000000000")),
                name="property_area_positive",
            ),
            models.CheckConstraint(
                condition=Q(land_area_m2__isnull=True)
                | Q(land_area_m2__gt=0, land_area_m2__lt=Decimal("1000000000000")),
                name="property_land_area_positive",
            ),
            models.CheckConstraint(
                condition=Q(rooms__isnull=True) | Q(rooms__gte=0),
                name="property_rooms_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(floors_total__isnull=True) | Q(floors_total__gt=0),
                name="property_floors_total_positive",
            ),
            models.CheckConstraint(
                condition=Q(floor__isnull=True)
                | Q(floors_total__isnull=True)
                | Q(floor__lte=F("floors_total")),
                name="property_floor_within_building",
            ),
        ]


class DealType(models.TextChoices):
    SALE = "sale"
    MONTH_RENT = "month_rent"
    DAY_RENT = "day_rent"


class MarketSegment(models.TextChoices):
    RESALE = "resale"
    NEW_BUILD = "new_build"


class Ad(models.Model):
    property = models.ForeignKey(Property, on_delete=models.PROTECT)
    source = models.ForeignKey(
        Source, on_delete=models.PROTECT, db_column="source_code", db_index=False
    )
    external_id = models.TextField()
    url = models.TextField()
    deal_type = models.TextField(choices=DealType.choices)
    market_segment = models.TextField(
        choices=MarketSegment.choices, null=True, blank=True
    )
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField()
    observed_at = models.DateTimeField()
    photos = models.JSONField(default=list, blank=True)
    photos_observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ads"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="ad_source_external_id_unique"
            ),
            models.CheckConstraint(
                condition=Q(external_id__regex=r"\S"), name="ad_external_id_present"
            ),
            models.CheckConstraint(
                condition=Q(url__regex=r"\S"), name="ad_url_present"
            ),
            models.CheckConstraint(
                condition=Q(deal_type__in=DealType.values), name="ad_deal_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(market_segment__isnull=True)
                | Q(market_segment__in=MarketSegment.values),
                name="ad_market_segment_valid",
            ),
            models.CheckConstraint(
                condition=Func(
                    F("photos"),
                    Value(
                        'strict $.type() == "array" && !(exists($[*] ? '
                        '(@.type() != "string" || !(@ like_regex '
                        '"^https?://[^/?#[:space:]]+[^[:space:]]*$" flag "i"))))'
                    ),
                    function="jsonb_path_match",
                    template="%(function)s(%(expressions)s::jsonpath)",
                    output_field=models.BooleanField(),
                ),
                name="ad_photos_url_array",
            ),
            models.CheckConstraint(
                condition=Q(photos_observed_at__isnull=False) | Q(photos=[]),
                name="ad_unknown_photos_empty",
            ),
            models.CheckConstraint(
                condition=Q(photos_observed_at__isnull=True)
                | Q(photos_observed_at__lte=F("observed_at")),
                name="ad_photos_observed_in_time",
            ),
        ]


class PriceKind(models.TextChoices):
    LISTED = "listed"
    FROM = "from"


class PriceHistory(models.Model):
    ad = models.ForeignKey(Ad, on_delete=models.PROTECT, db_index=False)
    observed_at = models.DateTimeField()
    price_rub = models.DecimalField(max_digits=16, decimal_places=2)
    price_kind = models.TextField(choices=PriceKind.choices)

    class Meta:
        db_table = "price_history"
        constraints = [
            models.UniqueConstraint(
                fields=["ad", "observed_at"], name="price_history_ad_observed_unique"
            ),
            models.CheckConstraint(
                condition=Q(price_rub__gte=0, price_rub__lt=Decimal("100000000000000")),
                name="price_history_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(price_kind__in=PriceKind.values),
                name="price_history_kind_valid",
            ),
        ]
