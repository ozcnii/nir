"""One verified Cian route: new flats for sale in Samara Oblast."""

import json
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from playwright.sync_api import Page

from catalog.contracts import Snapshot, SnapshotKey
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


def read_card(page: Page, requested_url: str, observed_at: datetime) -> Snapshot:
    """Read the current page after navigation, without requesting media."""
    return parse_card(
        requested_url,
        observed_at,
        page.locator("script:not([src])").all_text_contents(),
        page.locator('script[type="application/ld+json"]').all_text_contents(),
        loaded_url=page.url,
    )
