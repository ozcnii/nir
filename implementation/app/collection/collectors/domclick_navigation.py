"""Read the ready result produced by Domclick 24's public paginator click."""

import re
import time
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Locator, Page, Request, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.collection.browser import check_stop
from app.collection.normalization import RejectedCard, as_object

_DAILY_HOST = "24.domclick.ru"
_DAILY_OFFERS_PATH = "/api/offers/ad/v1"
DAILY_PAGE_SIZE = 36


def remaining_ms(deadline: float) -> float:
    remaining = (deadline - time.monotonic()) * 1000
    if remaining <= 0:
        raise PlaywrightTimeout("Result page attempt deadline elapsed")
    return remaining


def _read_response(
    response: Response, region: str, category: str | None
) -> tuple[int, int, list[object]]:
    if not response.ok:
        raise RejectedCard(f"http_{response.status}")
    query = parse_qs(urlsplit(response.url).query, keep_blank_values=True)
    if (
        query.get("locationGuid") != [region]
        or query.get("adults") != ["1"]
        or query.get("categories", []) != ([category] if category else [])
        or query.keys() - {"locationGuid", "adults", "categories", "limit", "offset"}
    ):
        raise RejectedCard("listing_scope_changed")
    offset = query.get("offset", [])
    if (
        query.get("limit") != [str(DAILY_PAGE_SIZE)]
        or len(offset) != 1
        or re.fullmatch(r"[0-9]{1,12}", offset[0]) is None
        or int(offset[0]) % DAILY_PAGE_SIZE
    ):
        raise RejectedCard("unknown_listing_format")
    try:
        data = as_object(response.json())
    except ValueError as error:
        raise RejectedCard("unknown_listing_format") from error
    total, offers = data.get("total"), data.get("offers")
    if type(total) is not int or total < 0 or not isinstance(offers, list):
        raise RejectedCard("unknown_listing_format")
    return int(offset[0]) // DAILY_PAGE_SIZE + 1, total, cast(list[object], offers)


def _is_daily_request(request: Request) -> bool:
    parts = urlsplit(request.url)
    return (parts.scheme, parts.netloc, parts.path) == (
        "https",
        _DAILY_HOST,
        _DAILY_OFFERS_PATH,
    ) and request.method == "GET"


def read_clicked_page(
    page: Page,
    control: Locator,
    region: str,
    category: str | None,
    deadline: float,
    stopped: Callable[[], bool],
) -> tuple[int, int, list[object]]:
    def matches(request: Request) -> bool:
        if _is_daily_request(request):
            return True
        if not request.is_navigation_request() or request.frame != page.main_frame:
            return False
        response = request.response()
        return response is not None and response.status >= 400

    # Observe only a complete response to the normal click, never request it ourselves.
    control.click(trial=True, timeout=remaining_ms(deadline))
    check_stop(stopped)
    with page.expect_request_finished(
        matches, timeout=remaining_ms(deadline)
    ) as pending:
        check_stop(stopped)
        control.click(timeout=remaining_ms(deadline))
    response = pending.value.response()
    if response is None:
        raise RejectedCard("listing_not_ready")
    return _read_response(response, region, category)
