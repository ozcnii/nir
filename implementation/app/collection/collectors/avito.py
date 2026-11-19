"""Read Avito's confirmed 6.1 rental, sale and closed-card formats."""

import json
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import (
    Deal,
    Snapshot,
    SnapshotKey,
    is_publication_url,
)
from app.collection.collectors.avito_fields import normalize_avito_card
from app.collection.normalization import RejectedCard, as_object

_CARD_PATH = re.compile(
    r"/[a-z0-9_-]+/(kvartiry|komnaty|doma_dachi_kottedzhi)"
    r"/[a-z0-9_.-]+_([1-9][0-9]{0,19})/?"
)
_ORIGIN = "https://www.avito.ru"


def _identity(url: str) -> tuple[str, str] | None:
    if not is_publication_url(url):
        return None
    parts = urlsplit(url)
    match = _CARD_PATH.fullmatch(parts.path)
    if parts.scheme == "https" and parts.netloc.lower() == "www.avito.ru" and match:
        return match[1], match[2]
    return None


def card_key(url: str) -> str | None:
    identity = _identity(url)
    return identity[1] if identity else None


def same_card(requested_url: str, loaded_url: str) -> bool:
    identity = _identity(requested_url)
    return identity is not None and identity == _identity(loaded_url)


def parse_card(
    requested_url: str,
    observed_at: datetime,
    route: dict[str, object],
    *,
    loaded_url: str,
    page_title: str = "",
    removed_deal: Deal | None = None,
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
        and removed_deal is not None
    ):
        return {
            **key,
            "state": "removed",
            "url": loaded_url,
            "deal_type": removed_deal,
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
        family, key, loaded_url, buyer, page_title, listing_label
    )


def capture_card(
    page: Page,
    requested_url: str,
    observed_at: datetime,
    deadline: float,
    *,
    removed_deal: Deal | None = None,
    listing_price: tuple[str, str] | None = None,
) -> Snapshot:
    """Read the public fields of a loaded card within the navigation deadline."""
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
        """() => {
            if (document.readyState !== "complete") return false;

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
        timeout=remaining_ms,
    )
    try:
        data = as_object(
            json.loads(cast(str, handle.json_value()), parse_float=Decimal)
        )
    finally:
        handle.dispose()
    return parse_card(
        requested_url,
        observed_at,
        as_object(data.get("route")),
        loaded_url=cast(str, data["url"]),
        page_title=cast(str, data["title"]),
        removed_deal=removed_deal,
        listing_price=listing_price,
    )
