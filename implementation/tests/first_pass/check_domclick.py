"""Exercise preserved Domclick cards through Chromium and PostgreSQL."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from playwright.sync_api import Page, Route

from app.collection.browser import browser_page
from app.collection.tests.domclick_fixture import ROOM, native_proposal, ssr


def verify_domclick() -> None:
    from app.catalog.models import Ad, PriceHistory, Property

    # Arrange: replay the source-shaped room and then a contradictory ID.
    projection = native_proposal()
    current = projection
    current_url = ROOM
    ads = Ad.objects.filter(
        source_id="domclick", external_id="/card/sale__room__2071752356"
    )

    def serve_domclick(route: Route) -> None:
        if route.request.url != current_url:
            route.abort()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=f"<script>{ssr({'productCard': current})}</script>",
        )

    @contextmanager
    def fixture_page() -> Iterator[Page]:
        with browser_page() as page:
            page.route("**/*", serve_domclick)
            yield page

    def persisted_rows() -> tuple[list[Mapping[str, object]], ...]:
        return tuple(
            list(rows.order_by("pk").values())
            for rows in (
                Property.objects.all(),
                Ad.objects.all(),
                PriceHistory.objects.all(),
            )
        )

    initial_count = Property.objects.count()
    with patch("app.collection.one_card.browser_page", fixture_page):
        # Act
        call_command("collect", ROOM, stdout=StringIO())
        call_command("collect", ROOM, stdout=StringIO())

        # Assert
        saved = ads.get()
        assert Property.objects.count() == initial_count + 1
        assert saved.property.area_m2 == Decimal("9.20")
        assert saved.property.rooms == 1
        assert list(
            PriceHistory.objects.filter(ad=saved).values_list("price_rub", "price_kind")
        ) == [(Decimal("1400000.00"), "listed")]

        # Arrange: foreign identity and an otherwise valid same-key room
        # whose explicitly offered room set contradicts its structured count.
        room_set = {
            **cast(dict[str, object], projection["objectInfo"]),
            "description": (
                "Продам комнату № 2 площадью 9,2 м² и комнату № 3 "
                "площадью 10 м² одним лотом."
            ),
        }
        inconsistent_member = {
            **room_set,
            "description": (
                "Продам комнату № 2 площадью 9,2 м². "
                "Площадь этой комнаты № 2 — 7 м² в соседней квартире. "
                "Площадь предлагаемой комнаты № 2 — 10 м²."
            ),
        }
        inconsistent_set_total = {
            **room_set,
            "roomArea": None,
            "description": (
                "Продаются комнаты общей площадью 22,5 м² одним лотом. "
                "Общая площадь этих комнат — 20 м² в соседней квартире. "
                "Общая площадь предлагаемых комнат — 23 м²."
            ),
        }
        before_rejection = persisted_rows()
        for next_projection, reason in (
            (
                {**projection, "id": 2071752357, "priceInfo": {"price": 1}},
                "unavailable",
            ),
            (
                {**projection, "objectInfo": room_set, "priceInfo": {"price": 42}},
                "conflicting_room_count",
            ),
            (
                {
                    **projection,
                    "objectInfo": inconsistent_member,
                    "priceInfo": {"price": 43},
                },
                "conflicting_room_area",
            ),
            (
                {
                    **projection,
                    "roomsOffered": None,
                    "objectInfo": inconsistent_set_total,
                    "priceInfo": {"price": 44},
                },
                "conflicting_room_area",
            ),
        ):
            current = next_projection

            # Act
            try:
                call_command("collect", ROOM, stdout=StringIO())
            except CommandError as error:
                # Assert
                assert str(error) == reason
            else:
                raise AssertionError(f"collect should reject Domclick {reason}")

        # Assert
        assert persisted_rows() == before_rejection

        # Arrange: only the other apartment's room has a published measurement.
        foreign_room = {
            **room_set,
            "roomArea": None,
            "description": (
                "Продам комнату № 2. В соседней квартире: сдаётся комната № 2. "
                "Площадь этой комнаты № 2 — 11 м²."
            ),
        }
        price = {**cast(dict[str, object], projection["priceInfo"]), "price": 1350000}
        current = {**projection, "objectInfo": foreign_room, "priceInfo": price}

        # Act
        call_command("collect", ROOM, stdout=StringIO())

        # Assert
        updated = ads.get()
        assert (updated.id, updated.property_id) == (saved.id, saved.property_id)
        assert updated.property.area_m2 == Decimal("9.20")
        assert updated.property.address == saved.property.address
        assert updated.photos == saved.photos
        assert list(
            PriceHistory.objects.filter(ad=updated)
            .order_by("observed_at")
            .values_list("price_rub", flat=True)
        ) == [Decimal("1400000.00"), Decimal("1350000.00")]
        assert Property.objects.count() == initial_count + 1

        # Arrange: without an earlier measurement the same foreign value stays NULL.
        fresh_id = 2071752358
        current_url = ROOM.replace(str(projection["id"]), str(fresh_id))
        current = {**current, "id": fresh_id}

        # Act
        call_command("collect", current_url, stdout=StringIO())

        # Assert
        fresh = Ad.objects.get(
            source_id="domclick", external_id=f"/card/sale__room__{fresh_id}"
        )
        assert fresh.property_id != saved.property_id
        assert fresh.property.area_m2 is None
        assert Property.objects.count() == initial_count + 2

    print(
        "Domclick: own conflict leaves rows unchanged; foreign room area does not write"
    )

    # Arrange: replay one own uncounted group and later another party's offer.
    group_id = 2071752357
    group_key = f"/card/sale__room__{group_id}"
    current_url = ROOM.replace(str(projection["id"]), str(group_id))
    own_offer = "Продаются комнаты общей площадью 22,5 м² одним лотом."
    group_info = {**room_set, "description": own_offer}
    group_price = {**cast(dict[str, object], projection["priceInfo"]), "price": 1300000}
    current = {
        **projection,
        "id": group_id,
        "roomsOffered": None,
        "objectInfo": group_info,
        "priceInfo": group_price,
    }
    group_ads = Ad.objects.filter(source_id="domclick", external_id=group_key)

    with patch("app.collection.one_card.browser_page", fixture_page):
        # Act
        call_command("collect", current_url, stdout=StringIO())
        accepted = group_ads.get()
        group_info["description"] = (
            "Продаются комнаты в соседней квартире. "
            "Общая площадь этих комнат — 23 м². "
            f"{own_offer}"
        )
        group_price["price"] = 1200000
        call_command("collect", current_url, stdout=StringIO())

    # Assert
    latest = group_ads.get()
    assert latest.id == accepted.id
    assert latest.property_id == accepted.property_id
    assert latest.property.rooms is None
    assert latest.property.area_m2 == Decimal("22.50")
    assert list(
        PriceHistory.objects.filter(ad=latest)
        .order_by("observed_at")
        .values_list("price_rub", flat=True)
    ) == [
        Decimal("1300000.00"),
        Decimal("1200000.00"),
    ]
    assert Property.objects.count() == initial_count + 3
    print("Domclick: neighbor total doesn't block the own group's price update")
