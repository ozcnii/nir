"""Read one supported public card and submit its snapshot to the catalog."""

from collections.abc import Callable
from datetime import datetime

from playwright.sync_api import Page

from app.catalog.contracts import Deal, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot, publication_deal
from app.catalog.references import source_enabled
from app.collection.browser import (
    CollectionError,
    browser_page,
    check_stop,
    read_page,
    without_media,
)
from app.collection.collectors import avito, cian, domclick
from app.collection.domclick_normalization import DailyListingQuote


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
    same_card = {
        "avito": avito.same_card,
        "cian": cian.same_card,
        "domclick": domclick.same_card,
    }[source]
    return read_page(
        page,
        url,
        stopped,
        lambda observed_at, deadline: capture_card(
            page, url, source, observed_at, deadline, removed_deal, quote=quote
        ),
        accepts_redirect=lambda loaded: same_card(url, loaded),
    )


def capture_card(
    page: Page,
    url: str,
    source: str,
    observed_at: datetime,
    deadline: float,
    removed_deal: Deal | None,
    *,
    quote: DailyListingQuote | None = None,
) -> Snapshot | SnapshotResult:
    if source == "avito":
        return avito.capture_card(
            page, url, observed_at, deadline, removed_deal=removed_deal
        )
    if source == "domclick":
        return domclick.read_card(
            page, url, observed_at, deadline=deadline, quote=quote
        )
    return cian.capture_card(page, url, observed_at, deadline)
