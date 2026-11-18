"""Read Avito's confirmed 6.1 rental, sale and closed-card formats."""

import json
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Page

from catalog.contracts import (
    Deal,
    Snapshot,
    SnapshotKey,
    is_http_url,
)
from collection.avito_normalization import normalize_avito_card
from collection.browser import NAVIGATION_TIMEOUT_MS
from collection.collectors.page_data import wait_for_card_data
from collection.normalization import RejectedCard, as_object

_CARD_PATH = re.compile(
    r"/[a-z0-9_-]+/(kvartiry|komnaty|doma_dachi_kottedzhi)"
    r"/[a-z0-9_.-]+_([1-9][0-9]{0,19})/?"
)
_ORIGIN = "https://www.avito.ru"


def _identity(url: str) -> tuple[str, str] | None:
    if not is_http_url(url):
        return None
    parts = urlsplit(url)
    match = _CARD_PATH.fullmatch(parts.path)
    if parts.scheme == "https" and parts.netloc.lower() == "www.avito.ru" and match:
        return match[1], match[2]
    return None


def card_key(url: str) -> str | None:
    identity = _identity(url)
    return identity[1] if identity else None


def parse_card(
    requested_url: str,
    observed_at: datetime,
    route: dict[str, object],
    *,
    loaded_url: str,
    page_title: str = "",
    expected_deal: Deal | None = None,
    listing_price: tuple[str, str] | None = None,
) -> Snapshot:
    identity = _identity(requested_url)
    if identity is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    family, external_id = identity
    key: SnapshotKey = {
        "source_code": "avito",
        "external_id": external_id,
        "observed_at": observed_at,
    }
    if _identity(loaded_url) != identity:
        return {**key, "state": "unavailable"}

    buyer = as_object(route.get("buyerItem"))
    if (
        route.get("buyerItem") is None
        and route.get("type") == "notFound"
        and route.get("message") == "Объявление закрыто."
        and expected_deal is not None
    ):
        return {
            **key,
            "state": "removed",
            "url": loaded_url,
            "deal_type": expected_deal,
        }
    item = as_object(buyer.get("item"))
    item_id = item.get("id")
    item_url = item.get("url")
    category = item.get("category")
    if (
        type(item_id) not in (int, str)
        or str(item_id) != external_id
        or item.get("isActive") is not True
        or (
            item_url is not None
            and (
                not isinstance(item_url, str)
                or not item_url.startswith("/")
                or _identity(_ORIGIN + item_url) != identity
            )
        )
        or (category is not None and as_object(category).get("slug") != family)
    ):
        return {**key, "state": "unavailable"}
    listing_label = (
        listing_price[1]
        if listing_price is not None and _identity(listing_price[0]) == identity
        else None
    )
    return normalize_avito_card(
        family, key, loaded_url, buyer, page_title, expected_deal, listing_label
    )


def read_card(
    page: Page,
    requested_url: str,
    observed_at: datetime,
    *,
    deadline: float | None = None,
    expected_deal: Deal | None = None,
    listing_price: tuple[str, str] | None = None,
) -> Snapshot:
    """Read a loaded card. The caller supplies trusted listing/deal context."""
    if _identity(requested_url) is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    if deadline is None:
        deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
    data = wait_for_card_data(
        page,
        r"""requested => {
            const expected = new URL(requested);
            const trimSlash = path => path.replace(/\/$/, "");
            const redirected = location.origin !== expected.origin
                || trimSlash(location.pathname) !== trimSlash(expected.pathname);
            if (!redirected && document.readyState !== "complete") return false;

            const route = window.__staticRouterHydrationData
                ?.loaderData?.["catalog-or-main-or-item"];
            const buyer = route?.buyerItem;
            const item = buyer?.item;
            const project = (value, fields) => {
                // Keep malformed arrays distinguishable without copying their contents.
                if (Array.isArray(value)) return [];
                return value && typeof value === "object"
                    ? Object.fromEntries(fields.map(field => [field, value[field]]))
                    : value;
            };
            return JSON.stringify({
                url: location.href,
                title: document.title,
                route: route && {
                    type: route.type,
                    message: route.message,
                    buyerItem: buyer && {
                        item: {
                            ...project(item, [
                                "id", "url", "address", "locationId", "title", "price",
                                "isActive", "description"
                            ]),
                            geo: project(item?.geo, ["coords"]),
                            category: project(item?.category, ["slug"]),
                            location: project(item?.location, ["slug"]),
                            formattedPrice: project(item?.formattedPrice, [
                                "value", "isHasValue", "formatedString", "postfix",
                                "currencySign"
                            ]),
                            imageUrls: Array.isArray(item?.imageUrls)
                                ? item.imageUrls.map(photo => project(photo, ["1280x960"]))
                                : item?.imageUrls,
                        },
                        ga: Array.isArray(buyer.ga)
                            ? [null, project(buyer.ga[1], ["type", "status", "offer_type"])]
                            : buyer.ga,
                        paramsDto: {items: Array.isArray(buyer.paramsDto?.items)
                            ? buyer.paramsDto.items.map(param =>
                                project(param, ["title", "description"]))
                            : buyer.paramsDto?.items},
                    }
                }
            });
        }""",
        deadline,
        arg=requested_url,
    )
    data = as_object(json.loads(cast(str, data), parse_float=Decimal))
    return parse_card(
        requested_url,
        observed_at,
        as_object(data.get("route")),
        loaded_url=cast(str, data["url"]),
        page_title=cast(str, data["title"]),
        expected_deal=expected_deal,
        listing_price=listing_price,
    )
