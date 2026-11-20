"""Read Cian primary offers, public next controls and verified unit groups."""

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
from app.collection.captcha import Challenge, ChallengePending
from app.collection.collectors.cian import card_key
from app.collection.listing_page import SearchPage
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
    ("Самарская область", "day_rent", APARTMENT_TYPE_ID): (
        "https://samara.cian.ru/snyat-kvartiru-posutochno-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "day_rent", ROOM_TYPE_ID): (
        "https://samara.cian.ru/snyat-komnatu-posutochno-samarskaya-oblast/",
        4608,
    ),
    ("Самарская область", "day_rent", HOUSE_TYPE_ID): (
        "https://samara.cian.ru/snyat-dom-posutochno-samarskaya-oblast/",
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

    def _catalog_query(self, url: str) -> dict[str, list[str]]:
        if not is_publication_url(url) or "\\" in url:
            return {}
        link = urlsplit(url)
        if (
            link.scheme != "https"
            or link.netloc not in (urlsplit(self.start).netloc, "www.cian.ru")
            or link.path != "/cat.php"
        ):
            return {}
        return parse_qs(link.query, keep_blank_values=True)

    def permits_next(self, url: str) -> bool:
        if self.deal == "day_rent":
            return url == self.start
        query = self._catalog_query(url)
        return (
            query.get("region") == [str(self.source_region)] and "multi_id" not in query
        )

    def group_key(self, url: str) -> str | None:
        if self.deal != "sale" or self.property_type_id != APARTMENT_TYPE_ID:
            return None
        query = self._catalog_query(url)
        group = query.get("multi_id", [])
        if (
            len(group) == 1
            and group[0].isascii()
            and group[0].isdigit()
            and not group[0].startswith("0")
            and query.get("deal_type") == ["sale"]
            and query.get("engine_version") == ["2"]
            and query.get("offer_type") == ["flat"]
            and query.get("region") in (None, [str(self.source_region)])
            and query.get("p") in (None, ["1"])
            and query.keys()
            <= {"multi_id", "deal_type", "engine_version", "offer_type", "region", "p"}
        ):
            return group[0]
        return None

    def matches_query(self, query: dict[str, object]) -> bool:
        house = self.property_type_id == HOUSE_TYPE_ID
        rent = self.deal != "sale"
        rental_mode = {"sale": None, "month_rent": "!1", "day_rent": "1"}[self.deal]
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
            and _value(query, "for_day") == rental_mode
        )


def route_for(
    region_id: int, deal: Deal, property_type_id: int, segment: str | None = None
) -> SearchRoute | None:
    # Published segment links exist, but the effective segment filter was not captured.
    if segment is not None:
        return None
    observed = _STARTS.get((REGION_NAMES.get(region_id, ""), deal, property_type_id))
    return SearchRoute(*observed, deal, property_type_id) if observed else None


class _Projection(TypedDict):
    captcha: bool
    query: dict[str, object] | None
    ids: list[object]
    total: object
    links: list[str]
    next: list[str]
    disabled: bool
    empty: bool
    groups: int
    next_buttons: int
    has_more: bool | None
    actual_page: int | None


def _value(query: dict[str, object], field: str) -> object:
    return as_object(query.get(field)).get("value")


def _projection(
    page: Page,
    deadline: float,
    *,
    daily: bool = False,
    previous_page: int | None = None,
) -> _Projection:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Result page attempt deadline elapsed")
    handle = page.wait_for_function(
        """({daily, previousPage}) => {
            const captcha = location.pathname === "/cian-captcha"
                || location.pathname.startsWith("/xpvnsulc")
                || document.title.toLowerCase().includes("captcha")
                || Boolean(document.querySelector(
                    '#js-challenge-loader'));
            if (!captcha && document.readyState !== "complete") return false;
            const entries = window._cianConfig?.["frontend-serp"];
            // Match the recorded SERP fields without searching unrelated content.
            const values = Array.isArray(entries)
                ? entries.map(entry => entry?.value)
                    .filter(value => value && typeof value === "object")
                    .flatMap(value => [value, ...Object.values(value)]) : [];
            const results = [...new Set(values)].filter(value => {
                const query = daily ? value?.query : value?.jsonQuery;
                if (!query || typeof query !== "object" || Array.isArray(query)) return false;
                return daily
                    ? value.list && Number.isInteger(value.list.page)
                    : Array.isArray(value.offers) && Number.isInteger(value.totalOffers);
            });
            const published = Array.isArray(entries)
                ? entries.some(entry => entry?.key === "defaultState" && entry.value != null
                    && (typeof entry.value !== "object" || Object.keys(entry.value).length > 0))
                : entries != null;
            if (!captcha && results.length === 0 && !published) return false;
            const result = results.length === 1 ? results[0] : null;
            const list = daily ? result?.list : null;
            if (!captcha && list && (list.status !== "success"
                    || (previousPage !== null && list.page === previousPage))) return false;
            const controls = Array.from(document.querySelectorAll("a[href], button"))
                .filter(element => element.textContent.trim() === "Дальше"
                    && element.getClientRects().length > 0);
            const disabled = element => element.disabled
                || element.getAttribute("aria-disabled") === "true";
            const enabled = controls.filter(element => !disabled(element));
            const links = Array.from(document.querySelectorAll("a[href]"),
                element => element.href);
            const offers = Array.isArray(result?.offers) ? result.offers : null;
            return {
                captcha,
                query: (daily ? result?.query : result?.jsonQuery) ?? null,
                ids: offers?.map(offer => offer?.id ?? null) ?? [],
                total: (daily ? list?.current : result?.totalOffers) ?? null,
                links,
                next: enabled.filter(element => element.tagName === "A")
                    .map(element => element.href),
                next_buttons: enabled.filter(element => element.tagName === "BUTTON").length,
                disabled: controls.length > 0 && controls.every(disabled),
                empty: document.body?.innerText.includes(daily
                    ? "Нет вариантов по вашему запросу" : "У нас ещё нет таких объявлений") ?? false,
                groups: Object.keys(result?.aggregatedOffers ?? {}).length,
                has_more: typeof list?.hasMore === "boolean" ? list.hasMore : null,
                actual_page: list?.page ?? null,
            };
        }""",
        arg={"daily": daily, "previousPage": previous_page},
        timeout=remaining_ms,
    )
    try:
        observed = cast(_Projection, handle.json_value())
    finally:
        handle.dispose()
    if observed["captcha"]:
        raise ChallengePending(
            Challenge("cian", "drag", "servicepipe", replaces_document=True)
        )
    return observed


def _card_links(observed: _Projection) -> tuple[set[str], dict[str, str]]:
    ids = {str(item) for item in observed["ids"] if type(item) is int and item > 0}
    urls: dict[str, str] = {}
    for url in observed["links"]:
        key = card_key(url)
        if key is not None and key in ids:
            urls.setdefault(key, url)
    return ids, urls


def read_page(
    page: Page, route: SearchRoute, deadline: float, previous_page: int | None = None
) -> SearchPage:
    daily = route.deal == "day_rent"
    observed = _projection(page, deadline, daily=daily, previous_page=previous_page)
    if page.url != route.start and not route.permits_next(page.url):
        raise RejectedCard("listing_scope_changed")
    query = observed["query"]
    if query is None:
        raise RejectedCard(
            "unknown_daily_listing_projection" if daily else "unknown_listing_format"
        )
    if not route.matches_query(query):
        raise RejectedCard("listing_scope_changed")
    number = (
        observed["actual_page"]
        if daily
        else _value(query, "page")
        if "page" in query
        else 1
    )
    if type(number) is not int or number < 1:
        raise RejectedCard("unknown_listing_format")

    ids, urls = _card_links(observed)
    next_targets = set(observed["next"])
    if len(next_targets) > 1 or observed["next_buttons"] > 1:
        raise RejectedCard("ambiguous_next")
    next_url = next(iter(next_targets), None)
    if next_url is not None and not route.permits_next(next_url):
        raise RejectedCard("listing_scope_changed")
    next_control = None
    if daily:
        if (
            observed["has_more"] is True
            and observed["next_buttons"] == 1
            and not next_url
        ):
            next_control = 'button:visible:text-is("Дальше"):not(:disabled):not([aria-disabled="true"])'
        elif observed["has_more"] is not False or observed["next_buttons"] or next_url:
            raise RejectedCard("unverified_listing_end")
    elif observed["next_buttons"]:
        raise RejectedCard("ambiguous_next")

    total = observed["total"]
    empty = (
        number == 1
        and total == 0
        and observed["empty"]
        and not ids
        and not (next_url or next_control)
    )
    if not empty and (
        not isinstance(total, int) or total < len(ids) or (not ids and not daily)
    ):
        raise RejectedCard("listing_not_ready")
    if (
        not daily
        and next_url is None
        and not (empty or observed["disabled"] or (number == 1 and total == len(ids)))
    ):
        raise RejectedCard("unverified_listing_end")

    groups = tuple(
        dict.fromkeys(
            link for link in observed["links"] if route.group_key(link) in ids
        )
    )
    incomplete: list[str] = []
    if (
        ids != urls.keys()
        or any(type(item) is not int for item in observed["ids"])
        or (daily and not empty and (not ids or total != len(ids)))
    ):
        incomplete.append("unresolved_listing_offers")
    if observed["groups"] > len({route.group_key(link) for link in groups}):
        incomplete.append("unexpanded_listing_group")
    return SearchPage(
        number,
        {key: value for key, value in query.items() if key != "page"},
        urls,
        next_url,
        tuple(incomplete),
        next_control=next_control,
        group_urls=groups,
    )


def read_group(
    page: Page, route: SearchRoute, deadline: float, *, group_url: str
) -> SearchPage:
    group = route.group_key(group_url)
    observed = _projection(page, deadline)
    query = observed["query"]
    if query is None:
        raise RejectedCard("unknown_listing_format")
    if (
        group is None
        or route.group_key(page.url) != group
        or query.get("_type") != "flatsale"
        or _value(query, "engine_version") != 2
        or _value(query, "region") not in (None, [route.source_region])
        or str(_value(query, "multi_id")) != group
        or _value(query, "page") not in (None, 1)
        or query.keys() - {"_type", "engine_version", "region", "multi_id", "page"}
    ):
        raise RejectedCard("listing_group_changed")

    ids, urls = _card_links(observed)
    complete = (
        bool(ids)
        and observed["total"] == len(ids) == len(observed["ids"])
        and ids == urls.keys()
        and not (observed["next"] or observed["next_buttons"] or observed["groups"])
    )
    # The published group page can lose its regional filter. Its unit cards
    # must still be checked against the frozen region by the collection runner.
    return SearchPage(
        1,
        query,
        urls,
        None,
        () if complete else ("unexpanded_listing_group",),
    )
