"""One verified Cian route: new flats for sale in Samara Oblast."""

import json
import re
import time
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from catalog.contracts import Snapshot, SnapshotKey
from collection.browser import NAVIGATION_TIMEOUT_MS
from collection.normalization import RejectedCard, as_object, normalize_cian_card

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


def _state(scripts: Iterable[str]) -> dict[str, object]:
    found: dict[str, object] | None = None
    for script in scripts:
        if not (marker := CONFIG.search(script)):
            continue
        try:
            entries, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(
                script, marker.end()
            )
        except (ValueError, RecursionError):
            continue
        if not isinstance(entries, list):
            continue
        for item in cast(list[object], entries):
            entry = as_object(item)
            if entry.get("key") == "defaultState":
                if found is not None:
                    return {}
                found = as_object(as_object(entry.get("value")).get("offerData"))
    return found or {}


def _product(scripts: Iterable[str], key: str) -> dict[str, object]:
    matches = []
    for script in scripts:
        try:
            product = as_object(json.loads(script, parse_float=Decimal))
        except (ValueError, RecursionError):
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
    requested_url: str,
    observed_at: datetime,
    config_scripts: Iterable[str],
    product_scripts: Iterable[str],
    *,
    loaded_url: str,
) -> Snapshot:
    external_id = card_key(requested_url)
    if external_id is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    key: SnapshotKey = {
        "source_code": "cian",
        "external_id": external_id,
        "observed_at": observed_at,
    }
    if card_key(loaded_url) != external_id:
        return {**key, "state": "unavailable"}
    data = _state(config_scripts)
    if not data:
        return {**key, "state": "unavailable"}

    return normalize_cian_card(
        loaded_url, key, data, _product(product_scripts, external_id)
    )


class EmbeddedPage(TypedDict):
    url: str
    captcha: bool
    config: list[str]
    products: list[str]


def capture_page(page: Page, url: str, deadline: float) -> EmbeddedPage:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
        r"""requested => {
            const expected = new URL(requested);
            const trimSlash = path => path.replace(/\/$/, "");
            const redirected = location.origin !== expected.origin
                || trimSlash(location.pathname) !== trimSlash(expected.pathname);
            const captcha = trimSlash(location.pathname) === "/cian-captcha"
                || document.title.toLowerCase().includes("captcha");
            // Bootstrap scripts can mention this card before its published data arrives.
            if (!redirected && !captcha && document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(script => !script.src);
            const config = scripts.map(script => script.textContent).filter(text =>
                text.includes("_cianConfig") && text.includes("frontend-offer-card")
            );
            if (!redirected && !captcha && config.length === 0) return false;
            return {
                url: location.href, captcha, config,
                products: scripts
                    .filter(script => script.type === "application/ld+json")
                    .map(script => script.textContent),
            };
        }""",
        arg=url,
        timeout=remaining_ms,
    )
    try:
        data = cast(EmbeddedPage, handle.json_value())
    finally:
        handle.dispose()
    if time.monotonic() >= deadline:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    return data


def read_card(page: Page, requested_url: str, observed_at: datetime) -> Snapshot:
    """Read an already loaded page without separate media requests."""
    data = capture_page(
        page, requested_url, time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
    )
    return parse_card(
        requested_url,
        observed_at,
        data["config"],
        data["products"],
        loaded_url=data["url"],
    )
