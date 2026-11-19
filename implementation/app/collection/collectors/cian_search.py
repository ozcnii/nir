"""Read primary offers and actual pagination from the observed Cian classic SERP."""

import time
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import Deal, is_publication_url
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    REGION_NAMES,
    ROOM_TYPE_ID,
)
from app.collection.collectors.cian import card_key
from app.collection.normalization import RejectedCard, as_object

# Published starting routes and effective region IDs in CIAN64-FINAL-PRIMARY.md.
_STARTS: dict[tuple[str, Deal, int], tuple[str, int]] = {
    ("Самарская область", "sale", APARTMENT_TYPE_ID): (
        "https://samara.cian.ru/kupit-kvartiru-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "sale", ROOM_TYPE_ID): (
        "https://samara.cian.ru/kupit-komnatu-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "sale", HOUSE_TYPE_ID): (
        "https://samara.cian.ru/kupit-dom-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "month_rent", APARTMENT_TYPE_ID): (
        "https://samara.cian.ru/snyat-kvartiru-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "month_rent", ROOM_TYPE_ID): (
        "https://samara.cian.ru/snyat-komnatu-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "month_rent", HOUSE_TYPE_ID): (
        "https://samara.cian.ru/snyat-dom-samarskaya-oblast/",
        4608,
    ),
    ("Саратовская область", "sale", APARTMENT_TYPE_ID): (
        "https://saratov.cian.ru/kupit-kvartiru-saratovskaya-oblast/",
        4609,
    ),
}


@dataclass(frozen=True)
class SearchRoute:
    start: str
    source_region: int
    deal: Deal
    property_type_id: int

    def permits_next(self, url: str) -> bool:
        if not is_publication_url(url) or "\\" in url:
            return False
        link = urlsplit(url)
        query = parse_qs(link.query, keep_blank_values=True)
        return (
            link.scheme == "https"
            and link.netloc in (urlsplit(self.start).netloc, "www.cian.ru")
            and link.path == "/cat.php"
            and query.get("region") == [str(self.source_region)]
            and "multi_id" not in query
        )

    def matches_query(self, query: dict[str, object]) -> bool:
        house = self.property_type_id == HOUSE_TYPE_ID
        rent = self.deal == "month_rent"
        return (
            query.get("_type")
            == (("suburban" if house else "flat") + ("rent" if rent else "sale"))
            and _value(query, "engine_version") == 2
            and query.keys()
            <= {
                "_type",
                "engine_version",
                "region",
                "room",
                "object_type",
                "for_day",
                "page",
            }
            and _value(query, "region") == [self.source_region]
            and _value(query, "room")
            == ([0] if self.property_type_id == ROOM_TYPE_ID else None)
            and _value(query, "object_type") == ([1] if house else None)
            and _value(query, "for_day") == ("!1" if rent else None)
        )


def route_for(region_id: int, deal: Deal, property_type_id: int) -> SearchRoute | None:
    observed = _STARTS.get((REGION_NAMES.get(region_id, ""), deal, property_type_id))
    return SearchRoute(*observed, deal, property_type_id) if observed else None


@dataclass(frozen=True)
class SearchPage:
    number: int
    filters: dict[str, object]
    urls: dict[str, str]
    next_url: str | None
    incomplete: tuple[str, ...]


class _Projection(TypedDict):
    captcha: bool
    query: dict[str, object] | None
    ids: list[object]
    total: object
    links: list[str]
    next: list[str]
    disabled: bool
    empty: bool
    groups: bool


def _value(query: dict[str, object], field: str) -> object:
    return as_object(query.get(field)).get("value")


def read_page(page: Page, route: SearchRoute, deadline: float) -> SearchPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Result page attempt deadline elapsed")
    handle = page.wait_for_function(
        """() => {
            const captcha = location.pathname === "/cian-captcha"
                || document.title.toLowerCase().includes("captcha");
            if (!captcha && document.readyState !== "complete") return false;
            const entries = window._cianConfig?.["frontend-serp"];
            // The captured result object has offers, totalOffers and jsonQuery.
            // Inspect item values and their direct children in the SERP config.
            const values = Array.isArray(entries)
                ? entries.map(entry => entry?.value)
                    .filter(value => value && typeof value === "object")
                    .flatMap(value => [value, ...Object.values(value)]) : [];
            const results = [...new Set(values)].filter(value =>
                value && Array.isArray(value.offers) && value.jsonQuery
                && Number.isInteger(value.totalOffers));
            const published = Array.isArray(entries)
                ? entries.some(entry => entry?.key === "defaultState" && entry.value != null
                    && (typeof entry.value !== "object" || Object.keys(entry.value).length > 0))
                : entries != null;
            if (!captcha && results.length === 0 && !published) return false;
            const result = results.length === 1 ? results[0] : null;
            const controls = Array.from(document.querySelectorAll("a[href], button"))
                .filter(element => element.textContent.trim() === "Дальше"
                    && element.getClientRects().length > 0);
            const disabled = element => element.disabled
                || element.getAttribute("aria-disabled") === "true";
            const links = Array.from(document.querySelectorAll("a[href]"),
                element => element.href);
            return {
                captcha,
                query: result?.jsonQuery ?? null,
                ids: result?.offers.map(offer => offer?.id ?? null) ?? [],
                total: result?.totalOffers ?? null,
                links,
                next: controls.filter(element => !disabled(element))
                    .map(element => element.href ?? ""),
                disabled: controls.length > 0 && controls.every(disabled),
                empty: document.body?.innerText.includes("У нас ещё нет таких объявлений") ?? false,
                groups: Object.keys(result?.aggregatedOffers ?? {}).length > 0,
            };
        }""",
        timeout=remaining_ms,
    )
    try:
        observed = cast(_Projection, handle.json_value())
    finally:
        handle.dispose()

    if observed["captcha"]:
        raise RejectedCard("source_captcha")
    if page.url != route.start and not route.permits_next(page.url):
        raise RejectedCard("listing_scope_changed")
    query = observed["query"]
    if query is None:
        raise RejectedCard("unknown_listing_format")
    if not route.matches_query(query):
        raise RejectedCard("listing_scope_changed")
    number = _value(query, "page") if "page" in query else 1
    if type(number) is not int or number < 1:
        raise RejectedCard("unknown_listing_format")

    ids = {str(item) for item in observed["ids"] if type(item) is int and item > 0}
    urls: dict[str, str] = {}
    for url in observed["links"]:
        if (key := card_key(url)) in ids:
            assert key is not None
            urls.setdefault(key, url)

    next_targets = set(observed["next"])
    if len(next_targets) > 1:
        raise RejectedCard("ambiguous_next")
    next_url = next(iter(next_targets), None)
    if next_url is not None and not route.permits_next(next_url):
        raise RejectedCard("listing_scope_changed")
    total = observed["total"]
    empty = (
        number == 1 and total == 0 and observed["empty"] and not ids and not next_url
    )
    if not empty and (not ids or not isinstance(total, int) or total < len(ids)):
        raise RejectedCard("listing_not_ready")
    if next_url is None and not (
        empty or observed["disabled"] or (number == 1 and total == len(ids))
    ):
        raise RejectedCard("unverified_listing_end")

    incomplete: list[str] = []
    if ids != urls.keys() or any(type(item) is not int for item in observed["ids"]):
        incomplete.append("unresolved_listing_offers")
    if observed["groups"]:
        incomplete.append("unexpanded_listing_group")
    return SearchPage(
        number,
        {key: value for key, value in query.items() if key != "page"},
        urls,
        next_url,
        tuple(incomplete),
    )
