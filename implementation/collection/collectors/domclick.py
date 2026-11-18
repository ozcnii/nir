"""Read, identify, and dispatch the supported Domclick card formats."""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page

from catalog.contracts import Deal, Snapshot, SnapshotKey
from collection.browser import NAVIGATION_TIMEOUT_MS
from collection.collectors.domclick_state import nuxt_card, ssr_state
from collection.collectors.nuxt_state import UNDEFINED
from collection.collectors.page_data import wait_for_card_data
from collection.domclick_normalization import DailyListingQuote as DailyListingQuote
from collection.domclick_normalization import (
    normalize_daily_card,
    normalize_native_card,
    normalize_partner_card,
)
from collection.normalization import RejectedCard, as_object

NATIVE_HOSTS = frozenset(
    (
        "domclick.ru",
        "samara.domclick.ru",
        "tolyatti.domclick.ru",
        "domodedovo.domclick.ru",
        "krasnogorsk.domclick.ru",
        "odincovo.domclick.ru",
        "podolsk.domclick.ru",
        "serpuxov.domclick.ru",
    )
)
NATIVE_PATH = re.compile(
    r"/card/((sale|rent)__(flat|new_flat|room|house|house_part|townhouse)__"
    r"([1-9][0-9]*))/?"
)
PARTNER_PATH = re.compile(r"(/detail/([1-9][0-9]*))/?")
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
DAILY_PATH = re.compile(r"(/card/(" + UUID + r"))/?")


@dataclass(frozen=True)
class CardIdentity:
    key: str
    published_id: str
    family: Literal["native", "new_flat", "partner", "daily"]
    deal: Deal
    native_type: str = ""


def _identity(url: str) -> CardIdentity | None:
    if any(char.isspace() or ord(char) < 32 or char == "\\" for char in url):
        return None
    try:
        parts = urlsplit(url)
        scheme = parts.scheme
        host = parts.hostname
        if (
            scheme not in ("http", "https")
            or parts.username is not None
            or parts.port not in (None, 443 if scheme == "https" else 80)
            or parts.netloc.casefold()
            not in (host, f"{host}:{443 if scheme == 'https' else 80}")
        ):
            return None
    except ValueError:
        return None
    if host == "domclick.sutochno.ru" and (match := PARTNER_PATH.fullmatch(parts.path)):
        return CardIdentity(match[1], match[2], "partner", "day_rent")
    if host == "24.domclick.ru" and (match := DAILY_PATH.fullmatch(parts.path)):
        return CardIdentity(match[1].lower(), match[2].lower(), "daily", "day_rent")
    if host in NATIVE_HOSTS and (match := NATIVE_PATH.fullmatch(parts.path)):
        key, deal, kind, published = match.groups()
        if deal == "rent" and kind == "new_flat":
            return None
        return CardIdentity(
            f"/card/{key}",
            published,
            "new_flat" if kind == "new_flat" else "native",
            "sale" if deal == "sale" else "month_rent",
            kind,
        )
    return None


def card_key(url: str) -> str | None:
    identity = _identity(url)
    return identity.key if identity else None


def _number_matches(value: object, identity: CardIdentity) -> bool:
    return type(value) is int and str(value) == identity.published_id


def read_daily_listing_quote(page: Page, card_url: str) -> DailyListingQuote | None:
    """Read the matching public snippet and its exact search context together."""
    identity = _identity(card_url)
    if identity is None or identity.family != "daily":
        return None
    listing = urlsplit(page.url)
    if listing.scheme != "https" or listing.netloc != "24.domclick.ru":
        return None
    if listing.path.startswith("/card/"):
        return None
    projection: object = page.evaluate(
        r"""() => {
            const date = document.querySelectorAll(
                "[data-e2e-id='search-form-datepicker'] input"
            );
            const guests = document.querySelectorAll(
                "[data-e2e-id='search-form-guests-control'] input"
            );
            if (date.length !== 1 || guests.length !== 1
                || !date[0].getClientRects().length
                || !guests[0].getClientRects().length) return null;
            const allSnippets = document.querySelectorAll("[data-e2e-id='snippet-item']");
            const snippets = Array.from(allSnippets).map(item => {
                const exact = selector => {
                    const found = item.querySelectorAll(selector);
                    return found.length === 1 && found[0].getClientRects().length
                        ? found[0].innerText : null;
                };
                const amount = exact("[data-e2e-id='short-rent-price']");
                const nightly = exact("[data-e2e-id='short-rent-price-per-night']");
                const links = Array.from(item.querySelectorAll("a[href]")).map(a => a.href);
                return {amount, nightly, links};
            });
            return {dates: date[0].value, guests: guests[0].value, snippets};
        }"""
    )
    context = as_object(projection)
    dates, guests, snippets = (
        context.get("dates"),
        context.get("guests"),
        context.get("snippets"),
    )
    if (
        not isinstance(dates, str)
        or not isinstance(guests, str)
        or not isinstance(snippets, list)
    ):
        return None
    matches = []
    for item in cast(list[object], snippets):
        snippet = as_object(item)
        links = snippet.get("links")
        if not isinstance(links, list):
            continue
        destinations = {
            destination: link
            for link in cast(list[object], links)
            if isinstance(link, str) and (destination := _identity(link)) is not None
        }
        if identity in destinations:
            if len(destinations) != 1:
                return None
            matches.append(
                (destinations[identity], snippet.get("amount"), snippet.get("nightly"))
            )
    if len(matches) != 1:
        return None
    link, amount, nightly = matches[0]
    if not isinstance(amount, str) or not isinstance(nightly, str):
        return None
    return DailyListingQuote(link, dates, guests, amount, nightly)


def _parse_native(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    data: dict[str, object],
    removed_marker: bool,
    page_title: str,
) -> Snapshot:
    expected = {
        "offerType": identity.native_type,
        "dealType": "sale" if identity.deal == "sale" else "rent",
    }
    if not _number_matches(data.get("id"), identity) or any(
        data.get(field) != value for field, value in expected.items()
    ):
        return {**key, "state": "unavailable"}
    status = data.get("status")
    if status == 8 and removed_marker:
        return {**key, "state": "removed", "url": url, "deal_type": identity.deal}
    if status != 2:
        return {**key, "state": "unavailable"}

    return normalize_native_card(
        key,
        url,
        data,
        native_type=identity.native_type,
        deal=identity.deal,
        page_title=page_title,
    )


def _parse_partner(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    nuxt_scripts: list[str],
) -> Snapshot:
    source = nuxt_card(nuxt_scripts, identity.published_id)
    if source is None:
        return {**key, "state": "unavailable"}
    state, detail = source
    try:
        if not _number_matches(state.field(detail, "id"), identity):
            return {**key, "state": "unavailable"}
        link = state.field(detail, "url")
        if (
            link is not None
            and link is not UNDEFINED
            and link
            != (f"https://sutochno.ru/front/searchapp/detail/{identity.published_id}")
        ):
            return {**key, "state": "unavailable"}
        status = state.field(detail, "status")
        if state.field(status, "name") != "approved":
            return {**key, "state": "unavailable"}
    except ValueError:
        return {**key, "state": "unavailable"}

    return normalize_partner_card(key, url, state, detail, deal=identity.deal)


def _parse_daily(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    source: dict[str, object],
    gallery: list[str],
    quote: DailyListingQuote | None,
    page_title: str,
) -> Snapshot:
    inventory = as_object(source.get("inventory"))
    search = as_object(inventory.get("searchData"))
    card_id = search.get("id")
    data = as_object(inventory.get("data"))
    if (
        not isinstance(card_id, str)
        or re.fullmatch(UUID, card_id) is None
        or card_id.lower() != identity.published_id
        or inventory.get("fetchStatus") != "success"
        or data.get("status") != "published"
    ):
        return {**key, "state": "unavailable"}
    if quote is not None and _identity(quote.card_url) != identity:
        quote = None
    return normalize_daily_card(
        key,
        url,
        data,
        deal=identity.deal,
        gallery=gallery,
        quote=quote,
        page_title=page_title,
    )


def parse_card(
    requested_url: str,
    observed_at: datetime,
    ssr_scripts: list[str],
    nuxt_scripts: list[str],
    *,
    loaded_url: str,
    removed_marker: bool = False,
    page_title: str = "",
    gallery: list[str] | None = None,
    quote: DailyListingQuote | None = None,
) -> Snapshot:
    identity = _identity(requested_url)
    if identity is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    key: SnapshotKey = {
        "source_code": "domclick",
        "external_id": identity.key,
        "observed_at": observed_at,
    }
    if _identity(loaded_url) != identity:
        return {**key, "state": "unavailable"}
    if identity.family == "partner":
        return _parse_partner(key, identity, loaded_url, nuxt_scripts)
    state = ssr_state(ssr_scripts)
    if identity.family == "daily":
        return _parse_daily(
            key, identity, loaded_url, state, gallery or [], quote, page_title
        )
    if identity.family == "native":
        return _parse_native(
            key,
            identity,
            loaded_url,
            as_object(state.get("productCard")),
            removed_marker,
            page_title,
        )
    # The saved observations prove this source branch and its publication ID.
    # Its complete per-field mapping still requires a source-shaped projection.
    return {**key, "state": "unavailable"}


class EmbeddedPage(TypedDict):
    ssr: list[str]
    nuxt: list[str]
    removed: bool
    title: str
    gallery: list[str]
    url: str


def read_card(
    page: Page,
    requested_url: str,
    observed_at: datetime,
    *,
    deadline: float | None = None,
    quote: DailyListingQuote | None = None,
) -> Snapshot:
    """Read the public documents already loaded by the collection runner."""
    if _identity(requested_url) is None:
        raise RejectedCard("invalid_identity")
    if deadline is None:
        deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
    data = wait_for_card_data(
        page,
        r"""() => {
            if (document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(s => !s.src);
            const text = document.body?.innerText ?? "";
            return {
                ssr: scripts.map(s => s.textContent)
                    .filter(s => /^\s*window\.__SSR_STATE__\s*=/.test(s)),
                nuxt: scripts.filter(s => s.id === "__NUXT_DATA__")
                    .map(s => s.textContent),
                removed: text.split("\n").some(line =>
                    line.replace(/\s+/g, " ").trim() === "Объявление снято с размещения"),
                title: document.title,
                gallery: Array.from(document.querySelectorAll(
                    "[data-e2e-id='gallery-widget'] img"
                )).map(img => img.currentSrc || img.src),
                url: location.href,
            };
        }""",
        deadline,
    )
    projected = cast(EmbeddedPage, data)
    return parse_card(
        requested_url,
        observed_at,
        projected["ssr"],
        projected["nuxt"],
        loaded_url=projected["url"],
        removed_marker=projected["removed"],
        page_title=projected["title"],
        gallery=projected["gallery"],
        quote=quote,
    )
