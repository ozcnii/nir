"""Open a headless Chromium page for the collector."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.collection.normalization import RejectedCard, as_object

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


class BrowserTransportError(PlaywrightError):
    pass


class DocumentResponseError(RejectedCard):
    def __init__(self, response: Response) -> None:
        super().__init__(f"http_{response.status}")
        self.url = response.url
        self.status = response.status


def check_stop(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise CollectionStopped


def without_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media"):
        route.abort()
    else:
        route.fallback()


@contextmanager
def browser_page() -> Iterator[Page]:
    with (
        sync_playwright() as playwright,
        playwright.chromium.launch(
            channel="chromium",
            headless=True,
            chromium_sandbox=True,
            handle_sigint=False,
            handle_sigterm=False,
        ) as browser,
        browser.new_context(accept_downloads=False) as context,
    ):
        yield context.new_page()


@contextmanager
def browser_post_status(page: Page, url: str) -> Iterator[Callable[[], int | None]]:
    """Observe HTTP errors that Chromium hides from cross-origin fetch."""
    session = page.context.new_cdp_session(page)
    requests: set[str] = set()
    failure: int | None = None

    def request_started(event: dict[str, object]) -> None:
        request = as_object(event.get("request"))
        identifier = event.get("requestId")
        if (
            isinstance(identifier, str)
            and request.get("url") == url
            and request.get("method") in ("POST", "OPTIONS")
        ):
            requests.add(identifier)

    def response_received(event: dict[str, object]) -> None:
        nonlocal failure
        identifier, status = event.get("requestId"), event.get("statusCode")
        if (
            isinstance(identifier, str)
            and identifier in requests
            and type(status) is int
        ):
            if 400 <= status < 600:
                failure = status

    try:
        session.on("Network.requestWillBeSent", request_started)
        session.on("Network.responseReceivedExtraInfo", response_received)
        session.send("Network.enable")
        yield lambda: failure
    finally:
        if not page.is_closed():
            session.detach()


def read_page[Result](
    page: Page,
    url: str | None,
    stopped: Callable[[], bool],
    capture: Callable[[datetime, float], Result],
    *,
    accepts_redirect: Callable[[str], bool],
    next_control: str | None = None,
    advance: Callable[[float], Result] | None = None,
) -> Result:
    responses: list[Response] = []
    if next_control is not None and advance is None:

        def document_response(response: Response) -> None:
            if (
                response.request.is_navigation_request()
                and response.frame == page.main_frame
            ):
                responses.append(response)

        page.on("response", document_response)
        try:
            return _read_page(
                page,
                url,
                stopped,
                capture,
                accepts_redirect,
                next_control,
                advance,
                responses,
            )
        finally:
            page.remove_listener("response", document_response)
    return _read_page(
        page, url, stopped, capture, accepts_redirect, next_control, advance, responses
    )


def _read_page[Result](
    page: Page,
    url: str | None,
    stopped: Callable[[], bool],
    capture: Callable[[datetime, float], Result],
    accepts_redirect: Callable[[str], bool],
    next_control: str | None,
    advance: Callable[[float], Result] | None,
    responses: list[Response],
) -> Result:
    direct_url = url if next_control is None else None
    clicked = False
    for retry in (False, True):
        check_stop(stopped)
        observed_at = datetime.now(UTC)
        deadline = time.monotonic() + CARD_TIMEOUT_MS / 1000
        try:
            if direct_url is not None:
                response = page.goto(
                    direct_url, wait_until="domcontentloaded", timeout=CARD_TIMEOUT_MS
                )
                check_stop(stopped)
                if not retry and response is not None and 500 <= response.status < 600:
                    continue
                if response is None:
                    raise CollectionError("no_http_response")
                if not response.ok:
                    raise CollectionError(f"http_{response.status}")
            elif advance is None and not clicked and next_control is not None:
                control = page.locator(next_control)
                control.click(trial=True, timeout=CARD_TIMEOUT_MS)
                check_stop(stopped)
                # After dispatch the same control can refer to another page.
                clicked = True
                control.click(timeout=max(1, (deadline - time.monotonic()) * 1000))
                if responses and not responses[-1].ok:
                    raise DocumentResponseError(responses[-1])
            check_stop(stopped)
            result = (
                advance(deadline)
                if advance is not None and direct_url is None
                else capture(observed_at, deadline)
            )
            check_stop(stopped)
            if time.monotonic() >= deadline:
                raise PlaywrightTimeout("Page attempt deadline elapsed")
            return result
        except RejectedCard as error:
            check_stop(stopped)
            if isinstance(error, DocumentResponseError):
                if (
                    not retry
                    and 500 <= error.status < 600
                    and accepts_redirect(error.url)
                ):
                    direct_url = error.url
                    continue
                raise CollectionError(str(error)) from error
            if not retry and str(error).startswith("http_5"):
                continue
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
                isinstance(error, (BrowserTransportError, PlaywrightTimeout))
                or any(code in str(error) for code in TRANSPORT_ERRORS)
            ):
                raise CollectionError("browser_read_failed") from error
    raise AssertionError("The final attempt returns or raises")
