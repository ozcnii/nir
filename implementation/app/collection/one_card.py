"""Read the supported Cian card and send its snapshot to the catalog."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import ActiveSnapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot
from app.catalog.references import source_enabled
from app.collection.browser import browser_page
from app.collection.collectors.cian import (
    capture_card,
    card_key,
)
from app.collection.collectors.cian_fields import RejectedCard

CARD_TIMEOUT_MS = 30_000
TRANSPORT_ERRORS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_ABORTED",
    "net::ERR_TIMED_OUT",
    "net::ERR_NETWORK_CHANGED",
)


class CollectionError(RuntimeError):
    pass


class CollectionStopped(Exception):
    pass


def _check_stop(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise CollectionStopped


def _without_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media"):
        route.abort()
    else:
        route.fallback()


def collect_cian_card(url: str, stopped: Callable[[], bool]) -> SnapshotResult:
    key = card_key(url)
    if key is None:
        raise CollectionError("unsupported_card_url")

    _check_stop(stopped)
    with run_session():
        if not source_enabled("cian"):
            raise CollectionError("cian_is_disabled")
        _check_stop(stopped)
        with browser_page() as page:
            page.route("**/*", _without_media)
            snapshot = _read_card(page, url, key, stopped)

        _check_stop(stopped)
        receipt = accept_snapshot(snapshot)
        if receipt.outcome not in ("created", "updated", "unchanged"):
            raise CollectionError(receipt.reason or receipt.outcome)
        return receipt


def _read_card(
    page: Page, url: str, key: str, stopped: Callable[[], bool]
) -> ActiveSnapshot:
    for retry in (False, True):
        _check_stop(stopped)
        observed_at = datetime.now(UTC)
        deadline = time.monotonic() + CARD_TIMEOUT_MS / 1000
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=CARD_TIMEOUT_MS
            )
            _check_stop(stopped)
            if not retry and response is not None and 500 <= response.status < 600:
                continue
            if response is None:
                raise CollectionError("no_http_response")
            if not response.ok:
                raise CollectionError(f"http_{response.status}")
            snapshot = capture_card(page, url, key, observed_at, deadline)
            _check_stop(stopped)
            if time.monotonic() >= deadline:
                raise PlaywrightTimeout("Card attempt deadline elapsed")
            return snapshot
        except RejectedCard as error:
            _check_stop(stopped)
            raise CollectionError(str(error)) from error
        except PlaywrightError as error:
            _check_stop(stopped)
            if retry or not (
                isinstance(error, PlaywrightTimeout)
                or any(code in str(error) for code in TRANSPORT_ERRORS)
            ):
                raise CollectionError("browser_read_failed") from error
    raise AssertionError("The final attempt returns or raises")
