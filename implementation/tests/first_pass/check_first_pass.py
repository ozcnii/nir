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
from app.collection.normalization import as_object
from app.collection.tests.avito_fixture import (
    projection as avito_projection,
)
from app.collection.tests.avito_fixture import (
    projection_html as avito_html,
)
from app.collection.tests.cian_fixture import projection, projection_html, script


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
    print(
        "Avito: terminal collection, repeat, wrong ID and explicit close verified in PostgreSQL"
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
                verify_avito()
