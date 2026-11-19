"""Read verified regional Domclick searches and their published layout groups."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import Deal, is_publication_url
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    REGION_NAMES,
    ROOM_TYPE_ID,
)
from app.collection.collectors.domclick import card_key, read_daily_listing_quote
from app.collection.collectors.domclick_navigation import (
    DAILY_PAGE_SIZE,
    read_clicked_page,
    remaining_ms,
)
from app.collection.collectors.domclick_state import ssr_state
from app.collection.listing_page import SearchPage
from app.collection.normalization import RejectedCard, as_object

_NATIVE_HOST = "samara.domclick.ru"
_DAILY_HOST = "24.domclick.ru"
# Effective aids in DOMCLICK64-BROWSER-CHECK and four exact subtype searches.
_NATIVE_REGION = 4987
_SEO_STARTS = {
    ("sale", APARTMENT_TYPE_ID): "/pokupka/kvartiry/samarskaya-oblast",
    ("month_rent", APARTMENT_TYPE_ID): "/arenda/kvartiry/samarskaya-oblast",
    ("sale", ROOM_TYPE_ID): "/pokupka/komnaty/samarskaya-oblast",
}
# Regional GeoObject values, independently published by Domclick 24.
_DAILY_REGIONS = {
    "Самарская область": "321b0daa-da95-4ce5-81b3-a7ab62d89d19",
    "Саратовская область": "9e840116-0390-4f05-a846-d4520716d1ce",
}
_DAILY_TYPES = {APARTMENT_TYPE_ID: "flat", ROOM_TYPE_ID: "room", HOUSE_TYPE_ID: "house"}
_NATIVE_POSITIONS = {"page", "offset"}
_DAILY_PAGINATOR = '[aria-label="Следующая"]'
_DAILY_NEXT = 'a[aria-label="Следующая"]'
_NATIVE_NEXT = '[data-e2e-id="paginate-next-btn"], a[aria-label="Следующая"]'
# DOMCLICK-KIND-ALIAS: the published offer_type filter calls new-build layouts
# "layout", while card keys keep the older "new_flat" segment.
_NATIVE_KIND_ALIASES = {"new_flat": "layout"}

_CARD_PREFIX = "/card/"
_GROUP_TYPE = "sale__new_flat__"
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class SearchRoute:
    start: str
    deal: Deal
    kinds: tuple[str, ...]
    source_region: int | str
    category: str | None = None

    def permits_next(self, url: str) -> bool:
        if not is_publication_url(url) or "\\" in url:
            return False
        start, next_page = urlsplit(self.start), urlsplit(url)
        if (next_page.scheme, next_page.netloc, next_page.path) != (
            start.scheme,
            start.netloc,
            start.path,
        ) or next_page.fragment:
            return False
        query = parse_qs(next_page.query, keep_blank_values=True)
        initial = parse_qs(start.query, keep_blank_values=True)
        for key in _NATIVE_POSITIONS:
            value = query.pop(key, None)
            initial.pop(key, None)
            if value is not None and (
                len(value) != 1 or re.fullmatch(r"[0-9]+", value[0]) is None
            ):
                return False
        # The public 24 next link deliberately omits geography. It must be clicked
        # in the current page and the effective published scope rechecked.
        return query == initial or (self.deal == "day_rent" and not query)

    def permits_card(self, url: str) -> bool:
        key = card_key(url)
        if key is None:
            return False
        kind = key.removeprefix(_CARD_PREFIX).split("__")
        if self.deal == "day_rent":
            return (
                urlsplit(url).netloc == _DAILY_HOST
                and _UUID.fullmatch(kind[0]) is not None
            )
        return (
            len(kind) == 3
            and kind[0] == ("sale" if self.deal == "sale" else "rent")
            and kind[1] in self.kinds
        )


def _native_form(deal: Deal, kind: str) -> SearchRoute:
    query = urlencode(
        {
            "deal_type": "sale" if deal == "sale" else "rent",
            "category": "living",
            "offer_type": kind,
            "aids": _NATIVE_REGION,
            "offset": 0,
        }
    )
    return SearchRoute(
        f"https://{_NATIVE_HOST}/search?{query}", deal, (kind,), _NATIVE_REGION
    )


def routes_for(
    region_id: int, deal: Deal, property_type_id: int, segment: str | None = None
) -> tuple[SearchRoute, ...]:
    region = REGION_NAMES.get(region_id)
    if segment is not None:
        return ()
    if deal == "day_rent":
        guid = _DAILY_REGIONS.get(region or "")
        kind = _DAILY_TYPES.get(property_type_id)
        if (
            guid is None
            or kind is None
            or (region != "Самарская область" and kind != "flat")
        ):
            return ()
        query = {"location_id": guid, "adt": "1"}
        category = kind if kind != "flat" else None
        if category:
            query["categories"] = category
        url = f"https://{_DAILY_HOST}/search?{urlencode(query)}"
        return (SearchRoute(url, deal, (kind,), guid, category),)
    if region != "Самарская область":
        # The recorded Moscow Oblast route also selects build_year >= 2022.
        return ()
    if property_type_id == HOUSE_TYPE_ID:
        return tuple(
            _native_form(deal, kind) for kind in ("house", "house_part", "townhouse")
        )
    if (path := _SEO_STARTS.get((deal, property_type_id))) is not None:
        kinds: tuple[str, ...] = (
            ("room",) if property_type_id == ROOM_TYPE_ID else ("flat",)
        )
        if deal == "sale" and property_type_id == APARTMENT_TYPE_ID:
            kinds = ("flat", "new_flat")
        return (
            SearchRoute(f"https://{_NATIVE_HOST}{path}", deal, kinds, _NATIVE_REGION),
        )
    if property_type_id == ROOM_TYPE_ID:
        return (_native_form(deal, "room"),)
    return ()


class _ListingDOM(TypedDict):
    scripts: list[str]
    url: str
    next_links: list[str]
    next_disabled: bool
    next_button: bool
    explicitly_empty: bool
    last: bool
    native_links: list[str]
    daily_links: list[str]


# The native capture places primary snippets and the paginator under one DOM
# wrapper. Take only preceding siblings of the paginator / terminal marker,
# then match their distinct cards to the published primary snippet count.
_LISTING_DOM = r"""({previous, daily, nextSelector, expected}) => {
    if (document.readyState !== "complete") return false;
    const visible = node => node.getClientRects().length > 0;
    const scripts = Array.from(document.scripts, script => script.textContent)
        .filter(text => /^\s*window\.__SSR_STATE__\s*=/.test(text));
    const controls = Array.from(document.querySelectorAll(nextSelector)).filter(visible);
    const disabled = node => node.disabled || node.getAttribute("aria-disabled") === "true"
        || [...node.classList].some(name => name.startsWith("pgnt-disabled"));
    const boundary = controls[0]?.closest('[class*="pgnt-root"], [data-e2e-id="paginate_root"]');
    const normalize = value => value.replace(/\s+/g, " ").trim();
    const marker = text => Array.from(document.querySelectorAll("main *, [role='main'] *, body > div *"))
        .find(node => node.children.length === 0 && visible(node) && normalize(node.textContent) === text);
    const empty = marker("Поиск не дал результатов");
    const last = marker("Сейчас это все объявления по вашему запросу");
    const links = node => [...(node.matches("a[href]") ? [node] : []), ...node.querySelectorAll("a[href]")]
        .filter(link => visible(link) && !link.closest("aside, nav, footer, [role='complementary']"))
        .map(link => link.href);
    let nativeLinks = [];
    if (!daily) {
        for (let pivot = boundary || last; pivot && pivot !== document.body; pivot = pivot.parentElement) {
            const siblings = [];
            for (let previous = pivot.previousElementSibling; previous; previous = previous.previousElementSibling) {
                siblings.unshift(previous);
            }
            const observed = siblings.flatMap(links).filter(url => new URL(url).pathname.startsWith("/card/"));
            if (observed.length) {
                nativeLinks = observed;
                break;
            }
        }
    }
    const root = document.querySelectorAll("#app > div > section.QrLw3 > div > div.N05bk");
    const dailyLinks = daily && root.length === 1 && !root[0].closest("section").hasAttribute("aria-label")
        ? links(root[0]) : [];
    if (expected) {
        const active = Array.from(document.querySelectorAll("a[class*='pgnt-selected']"))
            .filter(visible);
        const current = dailyLinks.some(url => expected.paths.includes(new URL(url).pathname.toLowerCase()));
        if (active.length !== 1 || normalize(active[0].textContent) !== String(expected.number)
            || !current) return false;
    }
    const signature = JSON.stringify([location.href, scripts, nativeLinks, dailyLinks, controls.length, Boolean(empty), Boolean(last)]);
    if (signature === previous) return false;
    return {
        signature,
        scripts,
        url: location.href,
        next_links: controls.filter(node => !disabled(node) && node.href).map(node => node.href),
        next_disabled: controls.length === 1 && disabled(controls[0]),
        next_button: controls.length === 1 && !controls[0].href && !disabled(controls[0]),
        ambiguous_next: controls.length > 1,
        explicitly_empty: Boolean(empty),
        last: Boolean(last),
        native_links: nativeLinks,
        daily_links: dailyLinks
    };
}"""


class _BrowserProjection(_ListingDOM):
    signature: str
    ambiguous_next: bool


def _native_filters(state: dict[str, object], route: SearchRoute) -> dict[str, object]:
    applied = as_object(as_object(state.get("filters")).get("applied"))
    kinds = applied.get("offer_type")
    expected_deal = "sale" if route.deal == "sale" else "rent"
    accepted = {
        frozenset(route.kinds),
        frozenset(_NATIVE_KIND_ALIASES.get(kind, kind) for kind in route.kinds),
    }
    if (
        not isinstance(kinds, list)
        or not all(isinstance(kind, str) for kind in kinds)
        or frozenset(kinds) not in accepted
        or applied.get("aids") != [route.source_region]
        or applied.get("deal_type") != expected_deal
        or applied.get("category") != "living"
        or any(
            value not in (None, "", [], {})
            for name, value in applied.items()
            if name not in {"aids", "deal_type", "category", "offer_type"}
        )
    ):
        raise RejectedCard("listing_scope_changed")
    return {
        "aids": applied["aids"],
        "deal_type": expected_deal,
        "category": "living",
        "offer_type": sorted(kinds),
    }


def _native_page(
    projection: _BrowserProjection, state: dict[str, object], route: SearchRoute
) -> SearchPage:
    filters = _native_filters(state, route)
    pagination = as_object(state.get("pagination"))
    offset, limit, total = (
        pagination.get("offset"),
        pagination.get("limit"),
        state.get("snippetsCount"),
    )
    if (
        type(limit) is not int
        or limit < 1
        or type(offset) is not int
        or offset < 0
        or offset % limit
    ):
        raise RejectedCard("unknown_listing_format")
    if type(total) is not int or total < 0:
        raise RejectedCard("unknown_listing_format")
    number = offset // limit + 1
    next_url = _next_link(projection, route)
    if total == 0:
        if (
            not projection["explicitly_empty"]
            or offset
            or next_url
            or projection["next_button"]
        ):
            raise RejectedCard("listing_not_ready")
        return SearchPage(number, filters, {}, None, ())
    expected = min(limit, total - offset)
    if expected < 1:
        raise RejectedCard("listing_not_ready")

    urls: dict[str, str] = {}
    groups: dict[str, str] = {}
    for url in projection["native_links"]:
        if route.permits_card(url) and (key := card_key(url)) is not None:
            urls.setdefault(key, url)
            if key.startswith(_CARD_PREFIX + _GROUP_TYPE) and parse_qs(
                urlsplit(url).query
            ).get("to_flats") == ["true"]:
                groups.setdefault(key, url)
    incomplete: list[str] = []
    if len(urls) != expected:
        urls.clear()
        groups.clear()
        incomplete.append("unresolved_listing_offers")
    next_control = _NATIVE_NEXT if projection["next_button"] else None
    if (
        next_url is None
        and next_control is None
        and not (
            offset + expected == total
            and (projection["last"] or projection["next_disabled"])
        )
    ):
        incomplete.append("unverified_listing_end")
    return SearchPage(
        number,
        filters,
        urls,
        next_url,
        tuple(incomplete),
        next_control,
        group_urls=tuple(groups.values()),
    )


def _next_link(projection: _BrowserProjection, route: SearchRoute) -> str | None:
    if projection["ambiguous_next"]:
        raise RejectedCard("ambiguous_next")
    links = projection["next_links"]
    if links and not route.permits_next(links[0]):
        raise RejectedCard("listing_scope_changed")
    return links[0] if links else None


def _daily_filters(route: SearchRoute) -> dict[str, object]:
    return {
        "locationId": route.source_region,
        "adt": 1,
        "categories": [route.category] if route.category else [],
    }


def _read_daily_page(
    page: Page,
    projection: _BrowserProjection,
    route: SearchRoute,
    number: int,
    total: int,
    ids: tuple[str, ...],
    entries: list[object],
    *,
    clicked: bool = False,
) -> SearchPage:
    next_url = _next_link(projection, route)
    filters = _daily_filters(route)
    if not ids:
        if total or number != 1 or not projection["explicitly_empty"] or next_url:
            raise RejectedCard("listing_not_ready")
        return SearchPage(number, filters, {}, None, ())
    if len(ids) > total or len({key.lower() for key in ids}) != len(ids):
        raise RejectedCard("unknown_listing_format")

    incomplete: list[str] = []
    urls: dict[str, str] = {}
    for key, raw in zip(ids, entries, strict=True):
        offer = as_object(raw)
        identifier, category = offer.get("id"), offer.get("category")
        if (
            not isinstance(identifier, str)
            or identifier.lower() != key.lower()
            or category not in _DAILY_TYPES.values()
        ):
            incomplete.append("unresolved_listing_offers")
            continue
        if category not in route.kinds:
            continue
        if offer.get("source") != "CAS":
            incomplete.append("unsupported_listing_offer")
            continue
        # SerpPage -> module 56285 -> CARD:/card/:id (one guest, no dates).
        path = f"{_CARD_PREFIX}{identifier.lower()}"
        urls[path] = f"https://{_DAILY_HOST}{path}"

    visible = {card_key(url) for url in projection["daily_links"]}
    quotes = {
        key: quote
        for key, url in urls.items()
        if key in visible
        and (
            quote := read_daily_listing_quote(
                page, url, response_ids=ids if clicked else None
            )
        )
        is not None
    }
    if not next_url and not (
        (number - 1) * DAILY_PAGE_SIZE + len(ids) == total
        and (projection["next_disabled"] or projection["last"])
    ):
        incomplete.append("unverified_listing_end")
    return SearchPage(
        number,
        filters,
        urls,
        None,
        tuple(dict.fromkeys(incomplete)),
        _DAILY_NEXT if next_url else None,
        quotes,
    )


def _daily_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(key, str) or _UUID.fullmatch(key) is None for key in value
    ):
        raise RejectedCard("unknown_listing_format")
    return tuple(cast(list[str], value))


def _daily_page(
    page: Page,
    projection: _BrowserProjection,
    state: dict[str, object],
    route: SearchRoute,
) -> SearchPage:
    query = as_object(state.get("searchData"))
    filters = _daily_filters(route)
    if (
        query.get("locationId") != filters["locationId"]
        or type(query.get("adt")) is not int
        or query["adt"] != filters["adt"]
        or query.get("categories", []) != filters["categories"]
        or any(query.get(date) is not None for date in ("dateStart", "dateEnd"))
        or state.get("outdatedSearch") is not False
        or state.get("invalidParams") is not False
    ):
        raise RejectedCard("listing_scope_changed")
    number, total = query.get("page"), state.get("total")
    if type(number) is not int or number < 1 or type(total) is not int or total < 0:
        raise RejectedCard("unknown_listing_format")
    ids = _daily_ids(state.get("ids"))
    entities = as_object(state.get("entities"))
    return _read_daily_page(
        page, projection, route, number, total, ids, [entities.get(key) for key in ids]
    )


def advance(
    page: Page,
    route: SearchRoute,
    previous_number: int,
    selector: str,
    deadline: float,
    stopped: Callable[[], bool],
) -> SearchPage:
    """Click the site's next control within a single attempt budget."""
    control = page.locator(selector)
    href = control.get_attribute("href", timeout=remaining_ms(deadline))
    target = urljoin(page.url, href) if href else ""
    if (
        not route.permits_next(target)
        or parse_qs(urlsplit(target).query).get("page") != [str(previous_number + 1)]
        or not isinstance(route.source_region, str)
    ):
        raise RejectedCard("listing_scope_changed")
    number, total, offers = read_clicked_page(
        page, control, route.source_region, route.category, deadline, stopped
    )
    last_page = (total + DAILY_PAGE_SIZE - 1) // DAILY_PAGE_SIZE
    if number <= previous_number or (
        not offers and number > last_page and last_page <= previous_number
    ):
        # Published SerpPage fallback returns to ceil(total/limit).
        raise RejectedCard("pagination_cycle")
    if number != previous_number + 1 or not offers or number > last_page:
        raise RejectedCard("listing_not_ready")
    ids = _daily_ids([as_object(offer).get("id") for offer in offers])
    if len(ids) != min(DAILY_PAGE_SIZE, total - (number - 1) * DAILY_PAGE_SIZE):
        raise RejectedCard("listing_not_ready")
    handle = page.wait_for_function(
        _LISTING_DOM,
        arg={
            "daily": True,
            "nextSelector": _DAILY_PAGINATOR,
            "expected": {
                "number": number,
                "paths": [f"{_CARD_PREFIX}{key.lower()}" for key in ids],
            },
        },
        timeout=remaining_ms(deadline),
    )
    try:
        projection = cast(_BrowserProjection, handle.json_value())
    finally:
        handle.dispose()
    if not route.permits_next(projection["url"]):
        raise RejectedCard("listing_scope_changed")
    return _read_daily_page(
        page, projection, route, number, total, ids, offers, clicked=True
    )


def read_page(
    page: Page, route: SearchRoute, deadline: float, previous_page: int | None = None
) -> SearchPage:
    daily = route.deal == "day_rent"
    signature = ""
    while (remaining_ms := (deadline - time.monotonic()) * 1000) > 0:
        handle = page.wait_for_function(
            _LISTING_DOM,
            arg={
                "previous": signature,
                "daily": daily,
                "nextSelector": _DAILY_PAGINATOR if daily else _NATIVE_NEXT,
            },
            timeout=remaining_ms,
        )
        try:
            projection = cast(_BrowserProjection, handle.json_value())
        finally:
            handle.dispose()
        signature = projection["signature"]
        if not route.permits_next(projection["url"]):
            raise RejectedCard("listing_scope_changed")
        script = ssr_state(projection["scripts"])
        state = as_object(script.get("serp" if daily else "search"))
        if not state:
            raise RejectedCard("unknown_listing_format")
        if daily:
            if state.get("fetchStatus") != "success":
                continue
            query = as_object(state.get("searchData"))
            if previous_page is not None and query.get("page") == previous_page:
                continue
            return _daily_page(page, projection, state, route)
        if state.get("status") != "loaded":
            continue
        location = parse_qs(urlsplit(projection["url"]).query)
        loaded = _native_page(projection, state, route)
        if previous_page is not None and loaded.number == previous_page:
            continue
        if "page" in location and location["page"] != [str(loaded.number)]:
            raise RejectedCard("listing_scope_changed")
        return loaded
    raise PlaywrightTimeout("Result page attempt deadline elapsed")


def read_group(
    page: Page, route: SearchRoute, deadline: float, *, group_url: str
) -> SearchPage:
    # DOMCLICK64-LAYOUT-PUBLISHED-JS: the site builds the path from flatId,
    # preserving the current query; the layout modal itself has no navigation.
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Group page attempt deadline elapsed")
    handle = page.wait_for_function(
        r"""() => {
            if (document.readyState !== "complete") return false;
            return Array.from(document.scripts, script => script.textContent)
                .filter(text => /^\s*window\.__SSR_STATE__\s*=/.test(text));
        }""",
        timeout=remaining_ms,
    )
    try:
        data = ssr_state(cast(list[str], handle.json_value()))
    finally:
        handle.dispose()
    key = card_key(group_url)
    flat = as_object(
        as_object(as_object(data.get("shell")).get("flatDetails")).get("data")
    )
    if not flat:
        raise RejectedCard("unknown_listing_format")
    identifier = flat.get("id")
    if (
        key is None
        or key != card_key(page.url)
        or not route.permits_card(page.url)
        or type(identifier) is not int
        or key != f"{_CARD_PREFIX}{_GROUP_TYPE}{identifier}"
    ):
        raise RejectedCard("listing_scope_changed")
    members = flat.get("flatsWithSameLayout")
    if not isinstance(members, list) or not members:
        return SearchPage(1, {}, {}, None, ("unexpanded_listing_group",))
    incomplete: tuple[str, ...] = ()
    urls: dict[str, str] = {}
    current = urlsplit(page.url)
    for member in members:
        child = as_object(member).get("flatId")
        if child == identifier:
            continue
        if type(child) is not int or child < 1:
            incomplete = ("unexpanded_listing_group",)
            continue
        child_key = f"{_CARD_PREFIX}{_GROUP_TYPE}{child}"
        if child_key in urls:
            incomplete = ("unexpanded_listing_group",)
        urls[child_key] = urlunsplit(
            (current.scheme, current.netloc, child_key, current.query, "")
        )
    return SearchPage(1, {}, urls, None, incomplete)
