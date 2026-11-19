"""Run a verified collection route and check its previously known publications."""

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager

from app.catalog.collection_queries import active_publications, scope_exists
from app.catalog.contracts import Deal, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot
from app.catalog.references import source_enabled
from app.collection.browser import browser_page
from app.collection.collectors.cian import card_key
from app.collection.collectors.cian_search import read_page as read_cian_search
from app.collection.collectors.cian_search import route_for
from app.collection.one_card import (
    CollectionError,
    check_stop,
    read_card,
    read_page,
    without_media,
)

_SOURCE_BLOCKS = {"http_401", "http_403", "http_429", "source_captcha"}


@contextmanager
def _catalog_worker() -> Iterator[ThreadPoolExecutor]:
    # Sync Playwright runs an event loop in the command thread. Keep Django's
    # connection and collection lock together on one worker throughout the run.
    with ThreadPoolExecutor(max_workers=1) as worker:
        session = ExitStack()
        try:
            worker.submit(session.enter_context, run_session()).result()
            yield worker
        finally:
            worker.submit(session.close).result()


def collect_search(
    source: str,
    region_id: int,
    property_type_id: int,
    deal: Deal,
    stopped: Callable[[], bool],
) -> dict[str, object]:
    route = route_for(region_id, deal, property_type_id) if source == "cian" else None
    if route is None:
        raise CollectionError("unsupported_collection_scope")

    check_stop(stopped)
    outcomes: Counter[str] = Counter()
    issues: Counter[str] = Counter()
    seen_keys: set[str] = set()
    seen_pages: set[int] = set()
    seen_urls: set[str] = set()
    with _catalog_worker() as catalog:
        if not catalog.submit(source_enabled, source).result():
            raise CollectionError(f"{source}_is_disabled")
        if not catalog.submit(scope_exists, region_id, property_type_id).result():
            raise CollectionError("unknown_collection_scope")
        known = catalog.submit(
            active_publications, source, region_id, property_type_id, deal
        ).result()

        def store(snapshot: Snapshot) -> SnapshotResult:
            check_stop(stopped)
            return accept_snapshot(snapshot)

        check_stop(stopped)
        with browser_page() as listing:
            listing.route("**/*", without_media)
            card = listing.context.new_page()
            card.route("**/*", without_media)

            def visit(key: str, url: str) -> None:
                check_stop(stopped)
                if key in seen_keys:
                    return
                seen_keys.add(key)
                if card_key(url) != key:
                    issues["unknown_card_identity"] += 1
                    return
                try:
                    snapshot = read_card(card, url, source, stopped, None)
                except CollectionError as error:
                    if str(error) in _SOURCE_BLOCKS:
                        raise
                    issues[str(error)] += 1
                    return
                if isinstance(snapshot, SnapshotResult):
                    receipt = snapshot
                elif snapshot["state"] == "active":
                    if (
                        snapshot["region_id"] != region_id
                        or snapshot["property_type_id"] != property_type_id
                        or snapshot["deal_type"] != deal
                    ):
                        issues["card_outside_scope"] += 1
                        return
                    check_stop(stopped)
                    receipt = catalog.submit(store, snapshot).result()
                elif snapshot["state"] == "removed" and key in known:
                    check_stop(stopped)
                    receipt = catalog.submit(store, snapshot).result()
                else:
                    issues["card_unavailable"] += 1
                    return
                outcomes[receipt.outcome] += 1
                if receipt.outcome not in (
                    "created",
                    "updated",
                    "unchanged",
                    "ignored",
                    "stale",
                ):
                    issues[receipt.reason or receipt.outcome] += 1

            listing_complete = True
            filters: dict[str, object] | None = None
            next_url: str | None = route.start
            try:
                while next_url is not None:
                    check_stop(stopped)
                    if next_url in seen_urls:
                        raise CollectionError("pagination_cycle")
                    seen_urls.add(next_url)
                    page = read_page(
                        listing,
                        next_url,
                        stopped,
                        lambda _, deadline: read_cian_search(listing, route, deadline),
                        accepts_redirect=lambda loaded: (
                            loaded == route.start or route.permits_next(loaded)
                        ),
                    )
                    if page.number in seen_pages:
                        raise CollectionError("pagination_cycle")
                    if page.number != len(seen_pages) + 1 or (
                        filters is not None and page.filters != filters
                    ):
                        raise CollectionError("listing_scope_changed")
                    filters = page.filters
                    seen_pages.add(page.number)
                    if page.incomplete:
                        listing_complete = False
                        issues.update(page.incomplete)
                    for key, url in page.urls.items():
                        visit(key, url)
                    next_url = page.next_url

                if listing_complete:
                    for key, url in known.items():
                        visit(key, url)
            except CollectionError as error:
                listing_complete = False
                issues[str(error)] += 1

    return {
        "outcome": "partial" if issues else "completed",
        "listing_complete": listing_complete,
        "pages": len(seen_pages),
        "unique_cards": len(seen_keys),
        "results": outcomes,
        "issues": issues,
    }
