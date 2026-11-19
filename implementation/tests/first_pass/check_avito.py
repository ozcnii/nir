"""Exercise preserved Avito cards through Chromium and PostgreSQL."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from playwright.sync_api import Page, Route

from app.collection.browser import browser_page
from app.collection.normalization import as_object
from app.collection.tests.avito_fixture import projection as avito_projection
from app.collection.tests.avito_fixture import projection_html as avito_html


def verify_avito() -> None:
    from app.catalog.models import Ad, PriceHistory, Property

    # Arrange: keep every page local to Playwright and use the restricted DB role.
    card = avito_projection("resale")
    url = card["url"]
    key = "8199518030"
    item = as_object(as_object(card["route"]["buyerItem"])["item"])
    publications = Ad.objects.filter(source_id="avito", external_id=key)

    def serve_card(route: Route) -> None:
        if route.request.url == url:
            route.fulfill(
                body=avito_html(card), content_type="text/html; charset=utf-8"
            )
        else:
            route.abort()

    @contextmanager
    def fixture_page() -> Iterator[Page]:
        with browser_page() as page:
            page.route("**/*", serve_card)
            yield page

    def collect() -> dict[str, object]:
        output = StringIO()
        with patch("app.collection.one_card.browser_page", fixture_page):
            call_command("collect", url, stdout=output)
        return as_object(json.loads(output.getvalue()))

    # Act: accept one publication, then read identical fields again.
    first, repeat = collect(), collect()

    # Assert: no duplicate object/link or price entry.
    saved = publications.get()
    assert first["outcome"] == "created" and repeat["outcome"] == "unchanged"
    assert first["ad_id"] == repeat["ad_id"] == saved.pk
    assert first["property_id"] == repeat["property_id"] == saved.property_id
    assert saved.property.area_m2 == Decimal("57.00")
    prices = PriceHistory.objects.filter(ad=saved)
    assert list(prices.values_list("price_rub", "price_kind")) == [
        (Decimal("9500000.00"), "listed")
    ]
    observed = saved.observed_at

    # Act: the same URL now carries another ID.
    item["id"] = int(key) + 1
    try:
        collect()
    except CommandError as error:
        assert str(error) == "unavailable"
    else:
        raise AssertionError("Another listing ID must not modify the saved listing")

    # Assert: neither timestamp, active state nor price changed.
    saved.refresh_from_db()
    assert saved.observed_at == observed
    assert saved.is_active and prices.count() == 1

    # Act: show the explicit closed-card marker for the known publication.
    card["route"] = avito_projection("closed")["route"]
    removed = collect()

    # Assert: only the previous active flag and observation time changed.
    closed = publications.get()
    assert removed["outcome"] == "updated" and removed["ad_id"] == closed.pk
    assert removed["property_id"] == closed.property_id == first["property_id"]
    assert not closed.is_active and closed.observed_at > observed
    assert prices.count() == 1
    # Arrange: an abstract share is a published card with an invalid unit.
    card = avito_projection("room")
    url = card["url"]
    counts = Ad.objects.count(), Property.objects.count(), PriceHistory.objects.count()

    # Act
    try:
        collect()
    except CommandError as error:
        assert str(error) == "unconfirmed_room_unit"
    else:
        raise AssertionError("An abstract share must not write a publication")

    # Assert
    assert counts == (
        Ad.objects.count(),
        Property.objects.count(),
        PriceHistory.objects.count(),
    )
    # Arrange: an advertised room explicitly conflicts with its structured area.
    item = as_object(as_object(card["route"]["buyerItem"])["item"])
    item.update(
        title="Доля: одна комната 12,5 м²",
        description="Квартира светлая. Площадь предлагаемой комнаты 9,2 м².",
    )

    # Act
    try:
        collect()
    except CommandError as error:
        assert str(error) == "conflicting_room_area"
    else:
        raise AssertionError("A conflicting offered room must not write a publication")

    # Assert
    assert counts == (
        Ad.objects.count(),
        Property.objects.count(),
        PriceHistory.objects.count(),
    )

    # Arrange: the exact closure marker also has a known public URL but no saved ad.
    card = avito_projection("closed")
    url = card["url"]

    # Act
    unknown = collect()

    # Assert: no rows or guessed deal.
    assert unknown == {
        "outcome": "not_found",
        "ad_id": None,
        "property_id": None,
        "price_changed": False,
    }
    assert counts == (
        Ad.objects.count(),
        Property.objects.count(),
        PriceHistory.objects.count(),
    )
    # Arrange: the same saved room shape now advertises a set with its own total.
    card = avito_projection("room")
    url = card["url"]
    card["title"] = "Две комнаты на продажу"
    buyer = as_object(card["route"]["buyerItem"])
    item = as_object(buyer["item"])
    item.update(
        title="Комната 12,5 м²",
        description=(
            "Продаются комнаты № 2 и № 3 одним лотом, общей площадью 22,5 м². "
            "Отдельно предлагаются комнаты № 4 и № 5. "
            "Общая площадь этих предлагаемых комнат — 23 м²."
        ),
    )
    params = as_object(buyer["paramsDto"])
    params["items"] = [
        row
        for row in cast(list[dict[str, object]], params["items"])
        if row["title"] != "Условия продажи"
    ]

    # Act
    bundle = collect()

    # Assert: the neighbor's total does not change the offered unit's total.
    bundle_ads = Ad.objects.filter(source_id="avito", external_id="7697142163")
    saved_bundle = bundle_ads.get()
    assert bundle["outcome"] == "created"
    assert saved_bundle.property.rooms == 2
    assert saved_bundle.property.area_m2 == Decimal("22.50")
    assert Ad.objects.count() == counts[0] + 1
    assert Property.objects.count() == counts[1] + 1

    bundle_properties = Property.objects.filter(pk=saved_bundle.property_id)
    bundle_prices = PriceHistory.objects.filter(ad=saved_bundle).order_by("pk")
    bundle_fields = bundle_ads.values().get()
    property_fields = bundle_properties.values().get()
    price_rows = list(bundle_prices.values())
    for description in (
        "Продаются комнаты № 2 и № 3 одним лотом, общей площадью 22,5 м². "
        "Общая площадь этих комнат — 23 м².",
        "Продаются комнаты общей площадью 22,5 м² одним лотом. "
        "Общая площадь этих комнат — 23 м².",
        "Продам комнату № 2 площадью 9 м² и комнату № 3 площадью 10 м² одним лотом. "
        "В другой квартире есть комната № 2. "
        "Площадь предлагаемой комнаты № 2 11 м².",
    ):
        # Arrange: two incompatible areas of the same bundle or the same member.
        item["description"] = description

        # Act
        try:
            collect()
        except CommandError as error:
            assert str(error) == "conflicting_room_area"
        else:
            raise AssertionError("Conflicting room areas must not update publication")

        # Assert: the previously accepted bundle and history retain their data.
        assert bundle_ads.values().get() == bundle_fields
        assert bundle_properties.values().get() == property_fields
        assert list(bundle_prices.values()) == price_rows
        assert (
            Ad.objects.count(),
            Property.objects.count(),
            PriceHistory.objects.count(),
        ) == tuple(count + 1 for count in counts)

    # Arrange: the own bundle has member sizes but no total in this repeat.
    # The second offer refers to a different room having the same number.
    item["description"] = (
        "Продам комнату № 2 площадью 9 м² и комнату № 3 площадью 10 м² одним лотом. "
        "Продаётся комната № 2 в соседней квартире. "
        "Площадь предлагаемой комнаты № 2 — 11 м²."
    )
    item["price"] = 750000
    as_object(item["formattedPrice"]).update(value=750000, formatedString="750 000 ₽")

    # Act
    updated = collect()

    # Assert: the available price advances, while the known total and IDs persist.
    assert updated == {
        "outcome": "updated",
        "ad_id": saved_bundle.pk,
        "property_id": saved_bundle.property_id,
        "price_changed": True,
    }
    assert bundle_properties.get().area_m2 == Decimal("22.50")
    assert bundle_ads.get().observed_at > saved_bundle.observed_at
    assert list(bundle_prices.values_list("price_rub", flat=True)) == [
        Decimal("752777.00"),
        Decimal("750000.00"),
    ]
    assert Ad.objects.count() == counts[0] + 1
    assert Property.objects.count() == counts[1] + 1

    # Arrange: a separate controlled key has no previous area to retain.
    url = url.replace("7697142163", "7697142164")
    item.update(id=7697142164, url=str(item["url"]).replace("7697142163", "7697142164"))

    # Act
    new_bundle = collect()

    # Assert: the foreign member does not supply an area for the new bundle.
    assert new_bundle["outcome"] == "created"
    created = Ad.objects.get(source_id="avito", external_id="7697142164")
    assert created.property_id != saved_bundle.property_id
    assert created.property.rooms == 2 and created.property.area_m2 is None
    print(
        "Avito: terminal collection, room bundle, repeat, wrong ID and explicit close verified in PostgreSQL"
    )
