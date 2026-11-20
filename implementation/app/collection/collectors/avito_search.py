"""Обход выдачи Авито: маршрут, чтение страницы, ссылки на карточки.

Поддержаны только проверенные сочетания региона, вида объекта и сделки: адрес
выдачи у Авито содержит код категории, который нельзя угадать. Новое сочетание
добавляется строкой в `_STARTS` после проверки вживую.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import Deal
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, REGION_NAMES
from app.collection.collectors.avito import card_key
from app.collection.listing_page import SearchPage
from app.collection.normalization import RejectedCard, as_object

# (название региона, сделка, вид объекта) -> адрес первой страницы выдачи.
_STARTS: dict[tuple[str, Deal, int], str] = {
    (
        "Самарская область",
        "sale",
        APARTMENT_TYPE_ID,
    ): "https://www.avito.ru/samara/kvartiry/prodam-ASgBAgICAUSSA8YQ?cd=1",
}

# Ссылки на карточки и признак следующей страницы читаются из отрисованного DOM:
# выдача Авито не публикует отдельного состояния.
_LISTING = r"""() => {
    if (document.readyState !== "complete") return null;
    const links = Array.from(document.querySelectorAll('a[href*="/kvartiry/"]'))
        .map(link => link.href.split('?')[0]);
    const urls = Array.from(new Set(links));
    const more = Array.from(document.querySelectorAll('a,button'))
        .some(node => /показать ещё|следующая страница/i.test(node.innerText || ''));
    return {url: location.href, urls, more};
}"""

_LISTING_WAIT_S = 30.0


@dataclass(frozen=True)
class SearchRoute:
    start: str
    source_region: int
    deal: Deal
    property_type_id: int

    def permits_next(self, url: str) -> bool:
        if "\\" in url:
            return False
        current, start = urlsplit(url), urlsplit(self.start)
        if (current.scheme, current.netloc, current.path) != (
            start.scheme,
            start.netloc,
            start.path,
        ):
            return False
        allowed = {"p", "cd"}
        return parse_qs(current.query, keep_blank_values=True).keys() <= allowed

    def permits_card(self, url: str) -> bool:
        return card_key(url) is not None


def route_for(
    region_id: int, deal: Deal, property_type_id: int, segment: str | None = None
) -> SearchRoute | None:
    if segment is not None:
        return None
    observed = _STARTS.get((REGION_NAMES.get(region_id, ""), deal, property_type_id))
    return (
        SearchRoute(observed, region_id, deal, property_type_id) if observed else None
    )


def _page_number(url: str) -> int:
    page_number = parse_qs(urlsplit(url).query).get("p")
    if not page_number or len(page_number) != 1 or not page_number[0].isdigit():
        return 1
    return int(page_number[0])


def _next_url(start: str, number: int) -> str:
    return f"{start}&p={number + 1}" if "?" in start else f"{start}?p={number + 1}"


def read_page(
    page: Page, route: SearchRoute, deadline: float, previous_page: int | None = None
) -> SearchPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Result page attempt deadline elapsed")
    handle = page.wait_for_function(
        _LISTING, timeout=min(remaining_ms, _LISTING_WAIT_S * 1000)
    )
    try:
        projection = as_object(cast(object, handle.json_value()))
    finally:
        handle.dispose()
    loaded = cast(str, projection.get("url", ""))
    if not route.permits_next(loaded):
        raise RejectedCard("listing_scope_changed")
    urls: dict[str, str] = {}
    for url in cast(list[object], projection.get("urls") or []):
        if not isinstance(url, str) or not route.permits_card(url):
            continue
        key = card_key(url)
        if key is not None:
            urls.setdefault(key, url)
    number = _page_number(loaded)
    if previous_page is not None and number == previous_page:
        raise RejectedCard("pagination_no_progress")
    if urls:
        return SearchPage(number, {}, urls, _next_url(route.start, number), ())
    incomplete = ("unresolved_listing_offers",) if projection.get("more") else ()
    return SearchPage(number, {}, {}, None, incomplete)
