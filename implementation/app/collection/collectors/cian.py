"""One verified Cian route: new flats for sale in Samara Oblast."""

import json
import re
import time
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.catalog.contracts import ActiveSnapshot, SnapshotKey
from app.collection.collectors.cian_fields import (
    RejectedCard,
    as_object,
    normalize_cian_card,
)

CARD_PATH = re.compile(r"/sale/flat/([1-9][0-9]{0,19})/?")
CONFIG = re.compile(
    r"""_cianConfig\[['"]frontend-offer-card['"]\].*?\.concat\(\s*""", re.DOTALL
)


def card_key(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        match = CARD_PATH.fullmatch(parts.path)
        if parts.scheme == "https" and parts.netloc == "samara.cian.ru" and match:
            return match[1]
    except ValueError:
        return None
    return None


class _CardPending(RejectedCard):
    """The document has not published the requested offer yet."""


def _state(scripts: Iterable[str], requested_id: str) -> dict[str, object]:
    found: dict[str, object] | None = None
    incompatible_offer = False
    for script in scripts:
        if not (marker := CONFIG.search(script)):
            continue
        try:
            entries, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(
                script, marker.end()
            )
        except (ValueError, InvalidOperation, RecursionError):
            continue
        if not isinstance(entries, list):
            continue
        for item in cast(list[object], entries):
            entry = as_object(item)
            if entry.get("key") != "defaultState":
                continue
            data = as_object(as_object(entry.get("value")).get("offerData"))
            offer = as_object(data.get("offer"))
            offer_id = offer.get("id")
            if (
                type(offer_id) is not int
                or str(offer_id) != requested_id
                or offer.get("status") != "published"
            ):
                incompatible_offer |= bool(offer)
                continue
            if found is not None:
                raise RejectedCard("no_published_card")
            found = data
    if found is None:
        reason = "invalid_card_direction" if incompatible_offer else "no_published_card"
        raise _CardPending(reason)
    return found


def _product(scripts: Iterable[str], key: str) -> dict[str, object]:
    matches = []
    for script in scripts:
        try:
            product = as_object(json.loads(script, parse_float=Decimal))
        except (ValueError, InvalidOperation, RecursionError):
            continue
        sku = product.get("sku")
        url = as_object(product.get("offers")).get("url")
        if (
            product.get("@type") == "Product"
            and isinstance(sku, (int, str))
            and str(sku) == key
            and isinstance(url, str)
            and card_key(url) == key
        ):
            matches.append(product)
    return matches[0] if len(matches) == 1 else {}


def parse_card(
    requested_id: str,
    observed_at: datetime,
    config_scripts: Iterable[str],
    product_scripts: Iterable[str],
    *,
    loaded_url: str,
) -> ActiveSnapshot:
    if card_key(loaded_url) != requested_id:
        raise RejectedCard("unexpected_redirect")
    data = _state(config_scripts, requested_id)
    key: SnapshotKey = {
        "source_code": "cian",
        "external_id": requested_id,
        "observed_at": observed_at,
    }
    return normalize_cian_card(
        loaded_url, key, data, _product(product_scripts, requested_id)
    )


class EmbeddedPage(TypedDict):
    url: str
    captcha: bool
    config: list[str]
    products: list[str]


def capture_card(
    page: Page, url: str, key: str, observed_at: datetime, deadline: float
) -> ActiveSnapshot:
    previous_config: list[str] = []
    while True:
        data = _wait_for_scripts(page, url, deadline, previous_config)
        if data["captcha"]:
            raise RejectedCard("source_captcha")
        try:
            return parse_card(
                key,
                observed_at,
                data["config"],
                data["products"],
                loaded_url=data["url"],
            )
        except _CardPending:
            previous_config = data["config"]


def _wait_for_scripts(
    page: Page, url: str, deadline: float, previous_config: list[str]
) -> EmbeddedPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
        r"""({requested, previousConfig}) => {
            const expected = new URL(requested);
            const trimSlash = path => path.replace(/\/$/, "");
            const redirected = location.origin !== expected.origin
                || trimSlash(location.pathname) !== trimSlash(expected.pathname);
            const captcha = trimSlash(location.pathname) === "/cian-captcha"
                || document.title.toLowerCase().includes("captcha");
            if (!redirected && !captcha && document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(script => !script.src);
            const config = scripts.map(script => script.textContent).filter(text =>
                text.includes("_cianConfig") && text.includes("frontend-offer-card")
            );
            const changed = config.length !== previousConfig.length
                || config.some((text, index) => text !== previousConfig[index]);
            if (!redirected && !captcha && !changed) return false;
            return {
                url: location.href, captcha, config,
                products: scripts
                    .filter(script => script.type === "application/ld+json")
                    .map(script => script.textContent),
            };
        }""",
        arg={"requested": url, "previousConfig": previous_config},
        timeout=remaining_ms,
    )
    try:
        data = cast(EmbeddedPage, handle.json_value())
    finally:
        handle.dispose()
    if time.monotonic() >= deadline:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    return data
