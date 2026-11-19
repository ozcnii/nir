"""Page through Cian's verified regional daily room search without UI clicks."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.parse import urljoin

from playwright.sync_api import Page, Request
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.collection.browser import BrowserTransportError, check_stop, read_page
from app.collection.collectors.cian_partner import partner_key
from app.collection.collectors.cian_search import SearchRoute
from app.collection.listing_page import SearchPage
from app.collection.normalization import RejectedCard, as_object

_MAP_URL = "https://api.cian.ru/dailyrent/web/v1/map/"
_OFFERS_URL = "https://api.cian.ru/dailyrent/web/v1/search/"
_MAP_FIELDS = {"zoom", "bbox", "jsonQuery", "queryString", "subdomain"}
_ROOM_BATCH_SIZE = 28  # The Cian listing reducer uses the map offersCount for Next.


@dataclass(frozen=True)
class RoomBatch:
    query: dict[str, object]
    query_string: str
    ids: tuple[int, ...]
    has_more: bool


def _remaining(deadline: float) -> float:
    remaining = (deadline - time.monotonic()) * 1000
    if remaining <= 0:
        raise PlaywrightTimeout("Cian room search attempt timed out")
    return remaining


def _check_document(page: Page, route: SearchRoute) -> None:
    if page.url == route.start:
        return
    if page.url.split("?", 1)[0].rstrip("/").endswith("/cian-captcha"):
        raise RejectedCard("source_captcha")
    raise RejectedCard("unexpected_redirect")


def _initial_map(
    page: Page, route: SearchRoute, deadline: float, stopped: Callable[[], bool]
) -> tuple[dict[str, object], dict[str, object]]:
    def is_map(request: Request) -> bool:
        return request.url == _MAP_URL and request.method == "POST"

    try:
        with page.expect_request_finished(
            is_map, timeout=_remaining(deadline)
        ) as pending:
            document = page.goto(
                route.start, wait_until="domcontentloaded", timeout=_remaining(deadline)
            )
            check_stop(stopped)
            if document is None:
                raise RejectedCard("no_http_response")
            if not document.ok:
                raise RejectedCard(f"http_{document.status}")
            _check_document(page, route)
    except PlaywrightTimeout:
        _check_document(page, route)
        raise
    request = pending.value
    response = request.response()
    check_stop(stopped)
    _check_document(page, route)
    if response is None:
        raise RejectedCard("no_http_response")
    if not response.ok:
        raise RejectedCard(f"http_{response.status}")
    body = as_object(request.post_data_json)
    query = as_object(body.get("jsonQuery"))
    if body.keys() != _MAP_FIELDS or not route.matches_query(query):
        raise RejectedCard("listing_scope_changed")
    if as_object(query.get("page")).get("value") != 1:
        raise RejectedCard("listing_scope_changed")
    try:
        return body, as_object(response.json())
    except ValueError as error:
        raise RejectedCard("unknown_listing_format") from error


def _post(
    page: Page, url: str, body: dict[str, object], deadline: float
) -> dict[str, object]:
    output = as_object(
        page.evaluate(
            """async ({url, body, timeout}) => {
                try {
                    const response = await fetch(url, {
                        method: "POST", headers: {"Content-Type": "application/json"},
                        body: JSON.stringify(body), redirect: "manual",
                        signal: AbortSignal.timeout(timeout)
                    });
                    if (response.type === "opaqueredirect") return {error: "redirect"};
                    if (!response.ok) return {status: response.status};
                    try { return {status: response.status, data: await response.json()}; }
                    catch (error) {
                        if (error.name === "TimeoutError" || error.name === "AbortError")
                            return {error: "timeout"};
                        return {error: "invalid_response"};
                    }
                } catch (error) {
                    if (error.name === "TimeoutError" || error.name === "AbortError")
                        return {error: "timeout"};
                    return {error: "network"};
                }
            }""",
            {"url": url, "body": body, "timeout": max(1, int(_remaining(deadline)))},
        )
    )
    error = output.get("error")
    if error == "timeout":
        raise PlaywrightTimeout("Cian room search attempt timed out")
    if error == "network":
        raise BrowserTransportError("Cian room search browser request failed")
    if error == "redirect":
        raise RejectedCard("unexpected_redirect")
    status = output.get("status")
    if type(status) is not int:
        raise RejectedCard("unknown_listing_format")
    if not 200 <= status < 300:
        raise RejectedCard(f"http_{status}")
    data = as_object(output.get("data"))
    if not data:
        raise RejectedCard("unknown_listing_format")
    return data


def _map_batch(route: SearchRoute, data: dict[str, object], number: int) -> RoomBatch:
    query = as_object(data.get("jsonQuery"))
    if not route.matches_query(query):
        raise RejectedCard("listing_scope_changed")
    effective_page = as_object(query.get("page")).get("value")
    if effective_page is not None and (
        type(effective_page) is not int or effective_page != number
    ):
        raise RejectedCard(
            "pagination_cycle"
            if type(effective_page) is int and effective_page < number
            else "listing_scope_changed"
        )

    raw = data.get("sortedOfferIds")
    count = data.get("offersCount")
    query_string = data.get("queryString")
    if (
        not isinstance(raw, list)
        or any(type(item) is not int or item < 1 for item in raw)
        or type(count) is not int
        or count != len(raw)
        or not isinstance(query_string, str)
        or not query_string
    ):
        raise RejectedCard("unknown_listing_format")
    ids = tuple(dict.fromkeys(cast(list[int], raw)))
    return RoomBatch(
        {key: value for key, value in query.items() if key != "page"},
        query_string,
        ids,
        count >= _ROOM_BATCH_SIZE,
    )


def _room_links(
    route: SearchRoute,
    batch: RoomBatch,
    response: dict[str, object],
) -> tuple[dict[str, str], bool]:
    query = as_object(response.get("jsonQuery"))
    echo_ids = query.get("ids")
    effective_scope = {key: value for key, value in query.items() if key != "ids"}
    if (
        not route.matches_query(effective_scope)
        or effective_scope != batch.query
        or (
            echo_ids is not None and as_object(echo_ids).get("value") != list(batch.ids)
        )
    ):
        raise RejectedCard("listing_scope_changed")
    offers = response.get("offers")
    if not isinstance(offers, list):
        raise RejectedCard("unknown_listing_format")
    expected = set(batch.ids)
    urls: dict[str, str] = {}
    resolved: set[int] = set()
    incomplete = False
    for raw_offer in offers:
        offer = as_object(raw_offer)
        identifier = offer.get("id")
        if (
            type(identifier) is not int
            or identifier not in expected
            or offer.get("cianId") != identifier
            or offer.get("category") != "dailyRoomRent"
            or offer.get("dealType") != "rent"
            or offer.get("offerType") != "flat"
        ):
            incomplete = True
            continue
        if offer.get("isCianPartner") is True:
            url = offer.get("externalOfferUrl")
            key = (
                partner_key(url)
                if isinstance(url, str) and url.startswith("https://")
                else None
            )
        elif offer.get("isCianPartner", False) is False:
            key = str(identifier)
            # The site's builder uses /{dealType}/{offerType}/{cianId}/.
            url = urljoin(route.start, f"/rent/flat/{identifier}/")
        else:
            key, url = None, None
        if key is not None and isinstance(url, str):
            urls.setdefault(key, url)
            resolved.add(identifier)
    return urls, incomplete or resolved != expected


def walk(
    page: Page, route: SearchRoute, stopped: Callable[[], bool]
) -> Iterator[SearchPage]:
    body: dict[str, object] | None = None

    def read_batch(page_number: int, deadline: float) -> tuple[SearchPage, bool]:
        nonlocal body
        if page_number == 1:
            body, map_response = _initial_map(page, route, deadline, stopped)
        else:
            assert body is not None
            _check_document(page, route)
            check_stop(stopped)
            query = {
                **as_object(body["jsonQuery"]),
                "page": {"type": "term", "value": page_number},
            }
            map_response = _post(page, _MAP_URL, {**body, "jsonQuery": query}, deadline)
        check_stop(stopped)
        _check_document(page, route)
        batch = _map_batch(route, map_response, page_number)
        if not batch.ids:
            return SearchPage(page_number, batch.query, {}, None, ()), False
        search = _post(
            page,
            _OFFERS_URL,
            {
                "jsonQuery": {
                    **batch.query,
                    "ids": {"type": "terms", "value": list(batch.ids)},
                },
                "queryString": batch.query_string,
                "subdomain": body["subdomain"],
            },
            deadline,
        )
        check_stop(stopped)
        _check_document(page, route)
        urls, incomplete = _room_links(route, batch, search)
        result = SearchPage(
            page_number,
            batch.query,
            urls,
            None,
            ("unresolved_listing_offers",) if incomplete else (),
        )
        return result, batch.has_more

    number = 1

    def capture(_: datetime, deadline: float) -> tuple[SearchPage, bool]:
        return read_batch(number, deadline)

    while True:
        result, has_more = read_page(
            page,
            None,
            stopped,
            capture,
            accepts_redirect=lambda url: url == route.start,
        )
        if number == 1 or result.urls or result.incomplete:
            yield result
        if not has_more:
            return
        number += 1
