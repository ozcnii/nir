"""The controlled single-card path used for the first end-to-end run."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route

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
    observed_at = datetime.now(UTC)
    try:
        response = work.navigate("card", url)
        if response is None or not response.ok:
            raise CardReadFailed(
                "http_unavailable", response.status if response is not None else None
            )
        target = urlsplit(page.url)
        if (
            target.hostname == "samara.cian.ru"
            and target.path.rstrip("/") == "/cian-captcha"
        ):
            raise CardReadFailed("source_captcha", response.status)
        try:
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
    work.checkpoint()
    return snapshot, work.write(lambda session: accept_snapshot(session, snapshot))
