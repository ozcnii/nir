"""Run verified collection routes and check their previously known publications."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from playwright.sync_api import Page

from app.catalog.collection_queries import active_publications, scope_exists
from app.catalog.contracts import Deal, Market, Snapshot, SnapshotResult
from app.catalog.locks import run_session
from app.catalog.publications import accept_snapshot
from app.catalog.reference_defaults import ROOM_TYPE_ID
from app.catalog.references import source_enabled
from app.collection.browser import (
    CollectionError,
    CollectionPace,
    browser_page,
    check_stop,
    collection_pace,
    read_page,
    wait_between,
    warm_profile,
    without_media,
)
from app.collection.captcha import Solver
from app.collection.collectors import (
    avito,
    avito_search,
    cian,
    cian_room_search,
    cian_search,
    domclick,
    domclick_search,
)
from app.collection.corpus import Corpus
from app.collection.domclick_normalization import DailyListingQuote
from app.collection.listing_page import SearchPage
from app.collection.normalization import RejectedCard
from app.collection.observations import Observe
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
    pages: (
        Callable[
            [Page, CollectionPace, int | None, Callable[[], bool]], Iterator[SearchPage]
        ]
        | None
    ) = None


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
                        lambda page, pace, limit, stopped: cian_room_search.walk(
                            page, route, stopped, pace, limit
                        )
                    )
                    if deal == "day_rent" and property_type_id == ROOM_TYPE_ID
                    else None,
                ),
            )
    if source == "avito":
        avito_route = avito_search.route_for(region_id, deal, property_type_id, segment)
        if avito_route is not None:
            return (
                Direction(
                    source,
                    avito_route.start,
                    avito_route.permits_next,
                    avito.card_key,
                    lambda page, deadline, previous: avito_search.read_page(
                        page, avito_route, deadline, previous_page=previous
                    ),
                    lambda page, deadline, address: avito_search.read_page(
                        page, avito_route, deadline
                    ),
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
    solver: Solver | None = None,
    captcha_root: Path | None = None,
) -> dict[str, object]:
    if not sources:
        raise CollectionError("collection_scope_required")
    pace = collection_pace()
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
                warm_profile(listing, routes[0].start, stopped)
                results[source] = _collect_source(
                    routes,
                    listing,
                    card,
                    known[source],
                    deal,
                    stopped,
                    store,
                    in_scope,
                    solver,
                    captcha_root,
                    pace,
                )

    scope = {
        "region_id": region_id,
        "property_type_id": property_type_id,
        "deal_type": deal,
        "market_segment": segment,
    }
    if len(results) == 1:
        source, result = next(iter(results.items()))
        return {**result, "scope": scope, "source": source}
    return {
        "outcome": (
            "completed"
            if all(row["outcome"] == "completed" for row in results.values())
            else "partial"
        ),
        "scope": scope,
        "sources": results,
    }


def collect_corpus(
    directory: Path,
    sources: Sequence[str],
    region_id: int,
    property_type_id: int,
    deal: Deal,
    stopped: Callable[[], bool],
    *,
    segment: Market | None = None,
    solver: Solver | None = None,
    captcha_root: Path | None = None,
) -> dict[str, object]:
    directions = {
        source: _directions(source, region_id, property_type_id, deal, segment)
        for source in dict.fromkeys(sources)
    }
    pace = collection_pace()
    corpus = Corpus(directory)
    check_stop(stopped)

    def in_scope(snapshot: Snapshot) -> bool:
        return snapshot["state"] == "active" and (
            snapshot["region_id"] == region_id
            and snapshot["property_type_id"] == property_type_id
            and snapshot["deal_type"] == deal
            and (segment is None or snapshot["market_segment"] == segment)
        )

    results = {}
    with browser_page() as listing:
        listing.route("**/*", without_media)
        card = listing.context.new_page()
        card.route("**/*", without_media)
        for source, routes in directions.items():
            check_stop(stopped)
            before = corpus.saved
            results[source] = _collect_source(
                routes,
                listing,
                card,
                {},
                deal,
                stopped,
                lambda _: SnapshotResult("created"),
                in_scope,
                solver,
                captcha_root,
                pace,
                corpus=corpus,
            )
            results[source]["saved"] = corpus.saved - before

    if len(results) == 1:
        source, result = next(iter(results.items()))
        return {**result, "source": source, "directory": str(directory)}
    return {
        "outcome": (
            "completed"
            if all(row["outcome"] == "completed" for row in results.values())
            else "partial"
        ),
        "sources": results,
        "saved": corpus.saved,
        "directory": str(directory),
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
    solver: Solver | None,
    captcha_root: Path | None,
    pace: CollectionPace,
    *,
    corpus: Corpus | None = None,
) -> dict[str, object]:
    results: Counter[str] = Counter()
    issues_by_stage: dict[str, Counter[str]] = defaultdict(Counter)
    skipped: dict[str, str] = {}
    seen_keys: set[str] = set()
    seen_groups: set[str] = set()
    cards_read = 0
    source = routes[0].source
    card_key = routes[0].card_key
    stage = "listing"

    def skip(key: str, reason: str) -> None:
        issues_by_stage[stage][reason] += 1
        skipped[key] = reason

    def visit(
        key: str,
        url: str,
        quote: DailyListingQuote | None = None,
        *,
        captured: Snapshot | SnapshotResult | None = None,
        recheck: bool = False,
        observe: Observe | None = None,
    ) -> None:
        nonlocal cards_read
        check_stop(stopped)
        if key in seen_keys:
            return
        seen_keys.add(key)
        if corpus is not None and corpus.contains(source, key):
            results["already_saved"] += 1
            return
        if card_key(url) != key:
            skip(key, "unknown_card_identity")
            return
        # The sampling limit bounds the listing walk. The removal recheck reads
        # a previously known set and stays paced but is not cut, or a known card
        # beyond the limit would never confirm that it was taken down.
        if not recheck and pace.max_cards is not None and cards_read >= pace.max_cards:
            raise CollectionError("stopped_by_limit")
        if captured is None:
            wait_between(pace.card_pause, stopped)
        cards_read += 1
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
                    solver=solver,
                    captcha_root=captcha_root,
                    observe=observe,
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
            page_limit = None if pace.max_pages is None else pace.max_pages - page_count
            if page_limit is not None and page_limit <= 0:
                listing_complete = False
                issues_by_stage[stage]["stopped_by_limit"] += 1
                break
            for page in _walk_route(
                direction, listing, stopped, solver, captcha_root, pace, page_limit
            ):
                page_count += 1
                observe = (
                    partial(
                        corpus.save, route=direction.start, page=page, in_scope=in_scope
                    )
                    if corpus is not None
                    else None
                )
                if page.incomplete:
                    listing_complete = False
                    issues_by_stage[stage].update(page.incomplete)
                for group_url in page.group_urls:
                    if group_url in seen_groups:
                        continue
                    seen_groups.add(group_url)
                    parent = card_key(group_url)
                    if parent in seen_keys or parent not in page.urls:
                        parent = None
                    if (
                        corpus is not None
                        and parent is not None
                        and corpus.contains(source, parent)
                    ):
                        parent = None
                    stage = "group"
                    if (
                        corpus is not None
                        and pace.max_cards is not None
                        and cards_read >= pace.max_cards
                    ):
                        raise CollectionError("stopped_by_limit")
                    try:
                        group, captured = _read_group(
                            direction,
                            card,
                            group_url,
                            stopped,
                            capture_parent=parent is not None,
                            removed_deal=deal if parent in known else None,
                            observe=observe,
                        )
                    except CollectionError as error:
                        if str(error) in _SOURCE_BLOCKS:
                            raise
                        listing_complete = False
                        if parent is None:
                            issues_by_stage[stage][str(error)] += 1
                        else:
                            seen_keys.add(parent)
                            skip(parent, str(error))
                        continue
                    if group.incomplete:
                        listing_complete = False
                        issues_by_stage[stage].update(group.incomplete)
                    if parent is not None:
                        visit(parent, group_url, captured=captured)
                    stage = "card"
                    for key, url in group.urls.items():
                        visit(key, url, group.quotes.get(key), observe=observe)
                stage = "card"
                for key, url in page.urls.items():
                    visit(key, url, page.quotes.get(key), observe=observe)
                stage = "listing"

        if listing_complete:
            stage = "known_card"
            for key, url in known.items():
                visit(key, url, recheck=True)
    except CollectionError as error:
        listing_complete = False
        issues_by_stage[stage][str(error)] += 1

    issues = sum(issues_by_stage.values(), Counter[str]())
    return {
        "outcome": "partial" if issues else "completed",
        "listing_complete": listing_complete,
        "pages": page_count,
        "unique_cards": len(seen_keys),
        "results": results,
        "issues": issues,
        "issues_by_stage": issues_by_stage,
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
    observe: Observe | None = None,
) -> tuple[SearchPage, Snapshot | SnapshotResult | None]:
    def capture(
        observed_at: datetime, deadline: float
    ) -> tuple[SearchPage, Snapshot | SnapshotResult | None]:
        group = direction.read_group(card, deadline, url)
        if not capture_parent:
            return group, None
        try:
            parent = capture_card(
                card,
                url,
                direction.source,
                observed_at,
                deadline,
                removed_deal,
                observe=observe,
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
    direction: Direction,
    listing: Page,
    stopped: Callable[[], bool],
    solver: Solver | None,
    captcha_root: Path | None,
    pace: CollectionPace,
    page_limit: int | None,
) -> Iterator[SearchPage]:
    if direction.pages is not None:
        yield from direction.pages(listing, pace, page_limit, stopped)
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

    count = 0
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
            solver=solver,
            captcha_root=captcha_root,
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
        count += 1
        yield page
        if next_url is None and next_control is None:
            return
        if page_limit is not None and count >= page_limit:
            raise CollectionError("stopped_by_limit")
        wait_between(pace.page_pause, stopped)
