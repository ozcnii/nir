"""Exercise the Django collect command with Chromium and a saved Cian card."""

import json
import os
import signal
import sys
from collections.abc import Iterator
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
    ads = Ad.objects.filter(
        source_id="domclick", external_id="/card/sale__room__2071752356"
    )

    def serve_domclick(route: Route) -> None:
        if route.request.url != ROOM:
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
        for next_projection, reason in (
            (
                {**projection, "id": 2071752357, "priceInfo": {"price": 1}},
                "unavailable",
            ),
            (
                {**projection, "objectInfo": room_set, "priceInfo": {"price": 42}},
                "conflicting_room_count",
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
    current_ad = ads.get()
    assert current_ad.id == saved.id
    assert current_ad.observed_at == saved.observed_at
    assert PriceHistory.objects.filter(ad=saved).count() == 1
    assert Property.objects.count() == initial_count + 1
    print(
        "Domclick: terminal collection, repeat and rejected snapshot no-write verified"
    )


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
