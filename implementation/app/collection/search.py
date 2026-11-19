"""Run verified collection routes and check their previously known publications."""

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime

from playwright.sync_api import Page

from app.catalog.collection_queries import active_publications, scope_exists
from app.catalog.contracts import Deal, Market, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot
from app.catalog.reference_defaults import ROOM_TYPE_ID
from app.catalog.references import source_enabled
from app.collection.browser import (
    CollectionError,
    browser_page,
    check_stop,
    read_page,
    without_media,
)
from app.collection.collectors import (
    cian,
    cian_room_search,
    cian_search,
    domclick,
    domclick_search,
)
from app.collection.domclick_normalization import DailyListingQuote
from app.collection.listing_page import SearchPage
from app.collection.normalization import RejectedCard
from app.collection.one_card import capture_card, read_card

_SOURCE_BLOCKS = {
    "http_401",
    "http_403",
    "http_429",
    "http_439",
    "source_captcha",
    "unknown_listing_format",
}


@dataclass(frozen=True)
class Direction:
    source: str
    start: str
    permits_next: Callable[[str], bool]
    card_key: Callable[[str], str | None]
    read: Callable[[Page, float, int | None], SearchPage]
    read_group: Callable[[Page, float, str], SearchPage]
    advance: (
        Callable[[Page, int, str, float, Callable[[], bool]], SearchPage] | None
    ) = None
    pages: Callable[[Page, Callable[[], bool]], Iterator[SearchPage]] | None = None


def _directions(
    source: str,
    region_id: int,
    property_type_id: int,
    deal: Deal,
    segment: Market | None,
) -> tuple[Direction, ...]:
    if source == "cian":
        if route := cian_search.route_for(region_id, deal, property_type_id, segment):
            return (
                Direction(
                    source,
                    route.start,
                    route.permits_next,
                    cian.card_key,
                    lambda page, deadline, previous: cian_search.read_page(
                        page, route, deadline, previous_page=previous
                    ),
                    lambda page, deadline, address: cian_search.read_group(
                        page, route, deadline, group_url=address
                    ),
                    pages=(
                        lambda page, stopped: cian_room_search.walk(
                            page, route, stopped
                        )
                    )
                    if deal == "day_rent" and property_type_id == ROOM_TYPE_ID
                    else None,
                ),
            )
    if source == "domclick":
        if routes := domclick_search.routes_for(
            region_id, deal, property_type_id, segment
        ):
            return tuple(_domclick_direction(source, route) for route in routes)
    raise CollectionError("unsupported_collection_scope")


def _domclick_direction(source: str, route: domclick_search.SearchRoute) -> Direction:
    return Direction(
        source,
        route.start,
        route.permits_next,
        domclick.card_key,
        lambda page, deadline, previous: domclick_search.read_page(
            page, route, deadline, previous_page=previous
        ),
        lambda page, deadline, address: domclick_search.read_group(
            page, route, deadline, group_url=address
        ),
        (
            lambda page, previous, selector, deadline, stopped: domclick_search.advance(
                page, route, previous, selector, deadline, stopped
            )
        )
        if route.deal == "day_rent"
        else None,
    )


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
    sources: Sequence[str],
    region_id: int,
    property_type_id: int,
    deal: Deal,
    stopped: Callable[[], bool],
    *,
    segment: Market | None = None,
) -> dict[str, object]:
    if not sources:
        raise CollectionError("collection_scope_required")
    directions = {
        source: _directions(source, region_id, property_type_id, deal, segment)
        for source in dict.fromkeys(sources)
    }
    check_stop(stopped)
    with _catalog_worker() as catalog:
        if not catalog.submit(scope_exists, region_id, property_type_id).result():
            raise CollectionError("unknown_collection_scope")
        known: dict[str, dict[str, str]] = {}
        for source in directions:
            check_stop(stopped)
            if not catalog.submit(source_enabled, source).result():
                raise CollectionError(f"{source}_is_disabled")
            known[source] = catalog.submit(
                active_publications, source, region_id, property_type_id, deal, segment
            ).result()

        def write(snapshot: Snapshot) -> SnapshotResult:
            check_stop(stopped)
            return accept_snapshot(snapshot)

        def store(snapshot: Snapshot) -> SnapshotResult:
            return catalog.submit(write, snapshot).result()

        def in_scope(snapshot: Snapshot) -> bool:
            return snapshot["state"] == "active" and (
                snapshot["region_id"] == region_id
                and snapshot["property_type_id"] == property_type_id
                and snapshot["deal_type"] == deal
                and (segment is None or snapshot["market_segment"] == segment)
            )

        check_stop(stopped)
        with browser_page() as listing:
            listing.route("**/*", without_media)
            card = listing.context.new_page()
            card.route("**/*", without_media)
            results = {}
            for source, routes in directions.items():
                check_stop(stopped)
                results[source] = _collect_source(
                    routes,
                    listing,
                    card,
                    known[source],
                    deal,
                    stopped,
                    store,
                    in_scope,
                )

    if len(results) == 1:
        return next(iter(results.values()))
    return {
        "outcome": (
            "completed"
            if all(row["outcome"] == "completed" for row in results.values())
            else "partial"
        ),
        "sources": results,
    }


def _collect_source(
    routes: tuple[Direction, ...],
    listing: Page,
    card: Page,
    known: dict[str, str],
    deal: Deal,
    stopped: Callable[[], bool],
    store: Callable[[Snapshot], SnapshotResult],
    in_scope: Callable[[Snapshot], bool],
) -> dict[str, object]:
    results: Counter[str] = Counter()
    issues: Counter[str] = Counter()
    skipped: dict[str, str] = {}
    seen_keys: set[str] = set()
    seen_groups: set[str] = set()
    source = routes[0].source
    card_key = routes[0].card_key

    def skip(key: str, reason: str) -> None:
        issues[reason] += 1
        skipped[key] = reason

    def visit(
        key: str,
        url: str,
        quote: DailyListingQuote | None = None,
        *,
        captured: Snapshot | SnapshotResult | None = None,
    ) -> None:
        check_stop(stopped)
        if key in seen_keys:
            return
        seen_keys.add(key)
        if card_key(url) != key:
            skip(key, "unknown_card_identity")
            return
        try:
            snapshot = (
                captured
                if captured is not None
                else read_card(
                    card,
                    url,
                    source,
                    stopped,
                    deal if key in known else None,
                    quote=quote,
                )
            )
        except CollectionError as error:
            reason = str(error)
            if reason in _SOURCE_BLOCKS:
                skipped[key] = reason
                raise
            skip(key, reason)
            return
        if isinstance(snapshot, SnapshotResult):
            receipt = snapshot
        elif (snapshot["source_code"], snapshot["external_id"]) != (source, key):
            skip(key, "unknown_card_identity")
            return
        elif snapshot["state"] == "active":
            if not in_scope(snapshot):
                skip(key, "card_outside_scope")
                return
            receipt = store(snapshot)
        elif (
            snapshot["state"] == "removed"
            and key in known
            and snapshot["deal_type"] == deal
        ):
            receipt = store(snapshot)
        else:
            skip(key, "card_unavailable")
            return
        results[receipt.outcome] += 1
        if receipt.outcome not in (
            "created",
            "updated",
            "unchanged",
            "ignored",
            "stale",
        ):
            skip(key, receipt.reason or receipt.outcome)

    listing_complete = True
    page_count = 0
    try:
        for direction in routes:
            for page in _walk_route(direction, listing, stopped):
                page_count += 1
                if page.incomplete:
                    listing_complete = False
                    issues.update(page.incomplete)
                for group_url in page.group_urls:
                    if group_url in seen_groups:
                        continue
                    seen_groups.add(group_url)
                    parent = card_key(group_url)
                    if parent in seen_keys or parent not in page.urls:
                        parent = None
                    try:
                        group, captured = _read_group(
                            direction,
                            card,
                            group_url,
                            stopped,
                            capture_parent=parent is not None,
                            removed_deal=deal if parent in known else None,
                        )
                    except CollectionError as error:
                        if str(error) in _SOURCE_BLOCKS:
                            raise
                        listing_complete = False
                        if parent is None:
                            issues[str(error)] += 1
                        else:
                            seen_keys.add(parent)
                            skip(parent, str(error))
                        continue
                    if group.incomplete:
                        listing_complete = False
                        issues.update(group.incomplete)
                    if parent is not None:
                        visit(parent, group_url, captured=captured)
                    for key, url in group.urls.items():
                        visit(key, url, group.quotes.get(key))
                for key, url in page.urls.items():
                    visit(key, url, page.quotes.get(key))

        if listing_complete:
            for key, url in known.items():
                visit(key, url)
    except CollectionError as error:
        listing_complete = False
        issues[str(error)] += 1

    return {
        "outcome": "partial" if issues else "completed",
        "listing_complete": listing_complete,
        "pages": page_count,
        "unique_cards": len(seen_keys),
        "results": results,
        "issues": issues,
        "skipped_cards": skipped,
    }


def _read_group(
    direction: Direction,
    card: Page,
    url: str,
    stopped: Callable[[], bool],
    *,
    capture_parent: bool,
    removed_deal: Deal | None,
) -> tuple[SearchPage, Snapshot | SnapshotResult | None]:
    def capture(
        observed_at: datetime, deadline: float
    ) -> tuple[SearchPage, Snapshot | SnapshotResult | None]:
        group = direction.read_group(card, deadline, url)
        if not capture_parent:
            return group, None
        try:
            parent = capture_card(
                card, url, direction.source, observed_at, deadline, removed_deal
            )
        except RejectedCard as error:
            if str(error) in _SOURCE_BLOCKS:
                raise
            parent = SnapshotResult("rejected", reason=str(error))
        return group, parent

    return read_page(
        card,
        url,
        stopped,
        capture,
        accepts_redirect=lambda loaded: loaded == url,
    )


def _walk_route(
    direction: Direction, listing: Page, stopped: Callable[[], bool]
) -> Iterator[SearchPage]:
    if direction.pages is not None:
        yield from direction.pages(listing, stopped)
        return

    seen_urls: set[str] = set()
    filters: dict[str, object] | None = None
    next_url: str | None = direction.start
    next_control: str | None = None
    previous_page: int | None = None

    def capture(_: datetime, deadline: float) -> SearchPage:
        return direction.read(
            listing, deadline, previous_page if next_control else None
        )

    def advance(deadline: float) -> SearchPage:
        assert (
            direction.advance is not None
            and previous_page is not None
            and next_control is not None
        )
        return direction.advance(
            listing, previous_page, next_control, deadline, stopped
        )

    while next_url is not None or next_control is not None:
        check_stop(stopped)
        address = next_url if next_url is not None else listing.url
        if next_control is None:
            if address in seen_urls:
                raise CollectionError("pagination_cycle")
            seen_urls.add(address)
        page = read_page(
            listing,
            address,
            stopped,
            capture,
            accepts_redirect=lambda loaded: (
                loaded == direction.start or direction.permits_next(loaded)
            ),
            next_control=next_control,
            advance=advance if direction.advance is not None and next_control else None,
        )
        if previous_page is not None and page.number <= previous_page:
            raise CollectionError("pagination_cycle")
        if page.number != (previous_page or 0) + 1 or (
            filters is not None and page.filters != filters
        ):
            raise CollectionError("listing_scope_changed")
        filters = page.filters
        next_url, next_control = page.next_url, page.next_control
        previous_page = page.number
        yield page
