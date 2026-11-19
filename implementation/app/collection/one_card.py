"""Read one supported public card and submit its snapshot to the catalog."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import Deal, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot, publication_deal
from app.catalog.references import source_enabled
from app.collection.browser import browser_page
from app.collection.collectors import avito, cian, domclick
from app.collection.domclick_normalization import DailyListingQuote
from app.collection.normalization import RejectedCard

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


def check_stop(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise CollectionStopped


def without_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media"):
        route.abort()
    else:
        route.fallback()


def collect_card(url: str, stopped: Callable[[], bool]) -> SnapshotResult:
    if (key := avito.card_key(url)) is not None:
        source = "avito"
    elif cian.card_key(url) is not None:
        source = "cian"
    elif domclick.card_key(url) is not None:
        source = "domclick"
    else:
        raise CollectionError("unsupported_card_url")

    check_stop(stopped)
    with run_session():
        if not source_enabled(source):
            raise CollectionError(f"{source}_is_disabled")
        check_stop(stopped)
        removed_deal = publication_deal(source, key) if key is not None else None
        with browser_page() as page:
            page.route("**/*", without_media)
            snapshot = read_card(page, url, source, stopped, removed_deal)

        check_stop(stopped)
        receipt = (
            snapshot
            if isinstance(snapshot, SnapshotResult)
            else accept_snapshot(snapshot)
        )
        if receipt.outcome not in ("created", "updated", "unchanged", "not_found"):
            raise CollectionError(receipt.reason or receipt.outcome)
        return receipt


def read_card(
    page: Page,
    url: str,
    source: str,
    stopped: Callable[[], bool],
    removed_deal: Deal | None,
    *,
    quote: DailyListingQuote | None = None,
) -> Snapshot | SnapshotResult:
    def capture(observed_at: datetime, deadline: float) -> Snapshot | SnapshotResult:
        if source == "avito":
            return avito.capture_card(
                page,
                url,
                observed_at,
                deadline,
                removed_deal=removed_deal,
            )
        if source == "domclick":
            return domclick.read_card(
                page,
                url,
                observed_at,
                deadline=deadline,
                quote=quote,
            )
        return cian.capture_card(page, url, observed_at, deadline)

    same_card = {
        "avito": avito.same_card,
        "cian": cian.same_card,
        "domclick": domclick.same_card,
    }[source]
    return read_page(
        page,
        url,
        stopped,
        capture,
        accepts_redirect=lambda loaded: same_card(url, loaded),
    )


def read_page[Result](
    page: Page,
    url: str,
    stopped: Callable[[], bool],
    capture: Callable[[datetime, float], Result],
    *,
    accepts_redirect: Callable[[str], bool],
    next_control: str | None = None,
) -> Result:
    for retry in (False, True):
        check_stop(stopped)
        observed_at = datetime.now(UTC)
        deadline = time.monotonic() + CARD_TIMEOUT_MS / 1000
        try:
            if next_control is None:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=CARD_TIMEOUT_MS
                )
                check_stop(stopped)
                if not retry and response is not None and 500 <= response.status < 600:
                    continue
                if response is None:
                    raise CollectionError("no_http_response")
                if not response.ok:
                    raise CollectionError(f"http_{response.status}")
            elif not retry:
                # A click may advance the listing before the ready state is rendered.
                page.locator(next_control).click(timeout=CARD_TIMEOUT_MS)
            check_stop(stopped)
            result = capture(observed_at, deadline)
            check_stop(stopped)
            if time.monotonic() >= deadline:
                raise PlaywrightTimeout("Page attempt deadline elapsed")
            return result
        except RejectedCard as error:
            check_stop(stopped)
            raise CollectionError(str(error)) from error
        except PlaywrightError as error:
            check_stop(stopped)
            if page.is_closed():
                raise
            if "Execution context was destroyed" in str(error) and not accepts_redirect(
                page.url
            ):
                raise CollectionError("unexpected_redirect") from error
            if retry or not (
                isinstance(error, PlaywrightTimeout)
                or any(code in str(error) for code in TRANSPORT_ERRORS)
            ):
                raise CollectionError("browser_read_failed") from error
    raise AssertionError("The final attempt returns or raises")
