"""Exercise the Django collect command with Chromium and a saved Cian card."""

import json
import os
import signal
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from threading import Event
from types import FrameType
from typing import cast
from unittest.mock import patch

import django
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from fixture_database import require_fixture_database
from playwright.sync_api import Page, Route

from app.catalog.locks import run_session
from app.collection.browser import browser_page
from app.collection.tests.cian_fixture import projection, projection_html, script
from app.collection.tests.domclick_fixture import ROOM, native_proposal, ssr


def expect_refusal(url: str, reason: str) -> None:
    # Arrange: a preflight refusal must happen before the browser opens.
    with patch(
        "app.collection.one_card.browser_page",
        side_effect=AssertionError("collect opened Chromium before refusing the run"),
    ):
        try:
            # Act
            call_command("collect", url)
        except CommandError as error:
            # Assert
            assert str(error) == reason
        else:
            raise AssertionError(f"collect should have refused the run: {reason}")


def verify(stop_on_signal: bool) -> None:
    from app.catalog.models import Ad, PriceHistory, Source

    url, offer, product = projection()
    if not stop_on_signal:
        expect_refusal("https://samara.cian.ru/", "unsupported_card_url")
        source = Source.objects.filter(pk="cian")
        source.update(enabled=False)
        try:
            expect_refusal(url, "cian_is_disabled")
        finally:
            source.update(enabled=True)
        print("collect: unsupported URL and disabled source refused before Chromium")

    # Arrange: serve the saved HTML under its original URL entirely inside Playwright.
    published = json.dumps(projection_html(offer, product)).replace("<", r"\u003c")
    stale_offer = {**offer, "id": cast(int, offer["id"]) - 1}
    html = (
        "<script>window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        '[{"key":"defaultState","value":{}}]);</script>'
        f"<script>{script(stale_offer)}</script>"
        f"<script>setTimeout(() => {{"
        f"document.body.insertAdjacentHTML('beforeend', {published});"
        f"}}, 700);</script>"
    )
    requests: list[str] = []

    def serve_card(route: Route) -> None:
        requests.append(route.request.url)
        if route.request.url != url:
            route.abort()
        elif len(requests) == 1:
            route.fulfill(
                status=503,
                content_type="text/html",
                body="<html><body>Temporary server error</body></html>",
            )
        else:
            route.fulfill(status=200, content_type="text/html", body=html)

    @contextmanager
    def fixture_page() -> Iterator[Page]:
        with browser_page() as page:
            page.route("**/*", serve_card)
            if stop_on_signal:
                stopped = Event()
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)

                def forward_stop(signum: int, frame: FrameType | None) -> None:
                    handler(signum, frame)
                    stopped.set()

                signal.signal(signal.SIGTERM, forward_stop)
                print("Collect command ready for SIGTERM", flush=True)
                try:
                    assert stopped.wait(30), "SIGTERM not received"
                finally:
                    signal.signal(signal.SIGTERM, handler)
            yield page

    publications = Ad.objects.filter(source_id="cian", external_id=str(offer["id"]))
    if stop_on_signal:
        # Arrange
        before = publications.values_list("observed_at", flat=True).get()

        # Act
        with patch("app.collection.one_card.browser_page", fixture_page):
            call_command("collect", url)

        # Assert
        assert requests == []
        assert publications.values_list("observed_at", flat=True).get() == before
        return

    # Arrange
    first_result = StringIO()
    second_result = StringIO()

    # Act
    with patch("app.collection.one_card.browser_page", fixture_page):
        call_command("collect", url, stdout=first_result)
        call_command("collect", url, stdout=second_result)

    # Assert: one retry, one publication, one property and one initial price.
    first = json.loads(first_result.getvalue())
    second = json.loads(second_result.getvalue())
    saved = publications.get()
    assert requests == [url, url, url]
    assert first["outcome"] == "created"
    assert second["outcome"] == "unchanged"
    assert first["ad_id"] == second["ad_id"] == saved.pk
    assert first["property_id"] == second["property_id"] == saved.property_id
    assert saved.property.area_m2 == Decimal("47.21")
    assert saved.photos == product["image"]
    assert list(
        PriceHistory.objects.filter(ad=saved).values_list("price_rub", "price_kind")
    ) == [(Decimal("7246735.00"), "listed")]
    print("Cian: retry, terminal collection and repeat verified in PostgreSQL")


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


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.config.settings")
    django.setup()
    with connection.cursor() as cursor:
        require_fixture_database(cursor)
    match sys.argv[1:]:
        case ["busy"]:
            url, _, _ = projection()
            expect_refusal(url, "collection or catalog operation already running")
            print("collect: second command refused before Chromium")
        case ["unlocked"]:
            with run_session():
                print("collect: another session acquired the key after SIGTERM")
        case args:
            verify(stop_on_signal=args == ["stop"])
            if not args:
                verify_domclick()
