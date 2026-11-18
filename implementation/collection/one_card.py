"""The controlled single-card path used for the first end-to-end run."""

import time
from datetime import datetime
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response, Route
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from psycopg import Connection
from psycopg.rows import TupleRow

from catalog.contracts import ActiveSnapshot
from collection.browser import BrowserFailed
from collection.collectors.cian import capture_page, card_key, parse_card
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

    def read(
        response: Response | None, observed_at: datetime, deadline: float
    ) -> tuple[ActiveSnapshot, int]:
        if response is None or not response.ok:
            raise CardReadFailed(
                "http_unavailable", response.status if response is not None else None
            )
        data = capture_page(page, url, deadline)
        if data["captcha"]:
            raise CardReadFailed("source_captcha", response.status)
        if card_key(data["url"]) != expected_key:
            raise CardReadFailed("unexpected_redirect", response.status)
        try:
            snapshot = parse_card(
                url,
                observed_at,
                data["config"],
                data["products"],
                loaded_url=data["url"],
            )
        except RejectedCard as error:
            raise CardReadFailed(str(error), response.status) from error
        if snapshot["state"] != "active":
            raise CardReadFailed("no_published_card", response.status)
        if time.monotonic() >= deadline:
            raise PlaywrightTimeout("Card attempt deadline elapsed")
        return snapshot, response.status

    try:
        snapshot, http_status = work.read_page("card", url, read)
    except PlaywrightTimeout as error:
        raise CardReadFailed("browser_attempt_timeout") from error
    except PlaywrightError as error:
        raise CardReadFailed("browser_read_failed") from error

    def store(session: Connection[TupleRow]) -> "Receipt":
        receipt = accept_snapshot(session, snapshot)
        if receipt.outcome not in ("created", "updated", "unchanged"):
            raise CardReadFailed(f"catalog_{receipt.outcome}", http_status)
        return receipt

    return snapshot, work.write(store)
