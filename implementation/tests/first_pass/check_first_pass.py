"""Exercise the Django collect command with Chromium and a saved Cian card."""

import json
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from functools import partial
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
    foreign_id = cast(int, offer["id"]) - 1
    stale_offer = {**offer, "id": foreign_id}
    stale_cian_id = {**offer, "cianId": foreign_id}
    html = (
        "<script>window._cianConfig = {};"
        "window._cianConfig['frontend-offer-card'] = [].concat("
        '[{"key":"defaultState","value":{}}]);</script>'
        f"<script>{script(stale_offer)}</script>"
        f"<script>{script(stale_cian_id)}</script>"
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
    verify_invalid_card(url, offer, product, saved.pk)


def verify_invalid_card(
    url: str, offer: dict[str, object], product: dict[str, object], ad_id: int
) -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: serve unsafe responses for a key with retained data.
    observed_before = Ad.objects.values_list("observed_at", flat=True).get(pk=ad_id)
    history_before = PriceHistory.objects.filter(ad_id=ad_id).count()
    redirect = url.replace(str(offer["id"]), "1234567890")
    foreign_id = int(redirect.rstrip("/").rsplit("/", 1)[1])
    offers = {
        "redirect": offer,
        "foreign_state": {**offer, "id": foreign_id, "cianId": foreign_id},
        "invalid_area": {**offer, "totalArea": "47.211"},
    }

    @contextmanager
    def response_page(case: str) -> Iterator[Page]:
        def serve(route: Route) -> None:
            if case == "redirect" and route.request.url == url:
                route.fulfill(status=302, headers={"location": redirect})
            else:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=(
                        (
                            f"<script>{script({})}</script>"
                            if case == "foreign_state"
                            else ""
                        )
                        + projection_html(offers[case], product)
                    ),
                )

        with browser_page() as page:
            page.route("**/*", serve)
            yield page

    # Act: reject foreign navigation, foreign state at the same URL and bad area data.
    outcomes = []
    for case in offers:
        try:
            with patch(
                "app.collection.one_card.browser_page", partial(response_page, case)
            ):
                call_command("collect", url, stdout=StringIO())
        except CommandError as error:
            outcomes.append(str(error))

    # Assert: no invalid response reaches an active write.
    assert outcomes == ["unavailable", "unavailable", "invalid_precision"], outcomes
    assert (
        Ad.objects.values_list("observed_at", flat=True).get(pk=ad_id)
        == observed_before
    )
    assert PriceHistory.objects.filter(ad_id=ad_id).count() == history_before
    assert not Ad.objects.filter(source_id="cian", external_id="1234567890").exists()
    print(
        "Cian: foreign URL/ID and malformed area leave publication and history untouched"
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
