"""The controlled single-card path used for the first end-to-end run."""

import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from psycopg import Connection
from psycopg.rows import TupleRow

from catalog.contracts import ActiveSnapshot
from collection.browser import BrowserFailed
from collection.collectors.cian import card_key, read_card
from collection.normalization import RejectedCard
from collection.runner import BrowserWork

if TYPE_CHECKING:
    from catalog.services import Receipt


class CardReadFailed(BrowserFailed):
    def __init__(self, problem: str, http_status: int | None = None) -> None:
        super().__init__("browser_operation_failed")
        self.problem = problem
        self.http_status = http_status


def _without_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media"):
        route.abort()
    else:
        route.fallback()


def collect_cian_card(work: BrowserWork, url: str) -> tuple[ActiveSnapshot, "Receipt"]:
    from catalog.services import accept_snapshot

    expected_key = card_key(url)
    if expected_key is None:
        raise CardReadFailed("unsupported_card_url")

    page = work.pages.card
    page.route("**/*", _without_media)
    try:
        response, observed_at, deadline = work.navigate("card", url)
        if response is None or not response.ok:
            raise CardReadFailed(
                "http_unavailable", response.status if response is not None else None
            )
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            raise CardReadFailed("no_published_card", response.status)
        try:
            page.wait_for_function(
                r"""requested => {
                    const expected = new URL(requested);
                    const trimSlash = path => path.replace(/\/$/, "");
                    return location.origin !== expected.origin
                        || trimSlash(location.pathname) !== trimSlash(expected.pathname)
                        || document.title.toLowerCase().includes("captcha")
                        || Array.from(document.scripts).some(script =>
                            !script.src && script.textContent.includes("frontend-offer-card")
                        );
                }""",
                arg=url,
                timeout=remaining_ms,
            ).dispose()
        except PlaywrightTimeout as error:
            raise CardReadFailed("no_published_card", response.status) from error
        target = urlsplit(page.url)
        if (
            target.hostname == "samara.cian.ru"
            and target.path.rstrip("/") == "/cian-captcha"
        ):
            raise CardReadFailed("source_captcha", response.status)
        try:
            if "captcha" in page.title().casefold():
                raise CardReadFailed("source_captcha", response.status)
            snapshot = read_card(page, url, observed_at)
        except RejectedCard as error:
            raise CardReadFailed(str(error), response.status) from error
    except PlaywrightError as error:
        raise CardReadFailed("browser_read_failed") from error
    if snapshot["state"] != "active":
        problem = (
            "unexpected_redirect"
            if card_key(page.url) != expected_key
            else "no_published_card"
        )
        raise CardReadFailed(problem, response.status)

    def store(session: Connection[TupleRow]) -> "Receipt":
        receipt = accept_snapshot(session, snapshot)
        if receipt.outcome not in ("created", "updated", "unchanged"):
            raise CardReadFailed(f"catalog_{receipt.outcome}", response.status)
        return receipt

    return snapshot, work.write(store)
