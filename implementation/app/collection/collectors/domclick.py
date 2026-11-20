"""Read, identify, and dispatch the supported Domclick card formats."""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit

from playwright.sync_api import Page

from app.catalog.contracts import Deal, Snapshot, SnapshotKey
from app.collection.captcha import Challenge, ChallengePending
from app.collection.collectors import domclick_observations
from app.collection.collectors.domclick_state import nuxt_card, ssr_state
from app.collection.collectors.nuxt_state import UNDEFINED
from app.collection.collectors.page_data import wait_for_card_data
from app.collection.domclick_normalization import DailyListingQuote as DailyListingQuote
from app.collection.domclick_normalization import (
    normalize_daily_card,
    normalize_native_card,
    normalize_new_flat_card,
    normalize_partner_card,
)
from app.collection.normalization import RejectedCard, as_object
from app.collection.observations import Observe, observe_publication

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


def same_card(requested_url: str, loaded_url: str) -> bool:
    expected = _identity(requested_url)
    return expected is not None and _identity(loaded_url) == expected


def _number_matches(value: object, identity: CardIdentity) -> bool:
    return type(value) is int and str(value) == identity.published_id


def _daily_id_matches(value: object, identity: CardIdentity) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(UUID, value) is not None
        and value.lower() == identity.published_id
    )


def read_daily_listing_quote(
    page: Page, card_url: str, *, response_ids: tuple[str, ...] | None = None
) -> DailyListingQuote | None:
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
            const roots = document.querySelectorAll("#app > div > section.QrLw3 > div > div.N05bk");
            if (roots.length !== 1 || roots[0].closest("aside, [role='complementary']")) return null;
            if (roots[0].closest("section").hasAttribute("aria-label")) return null;
            const scripts = Array.from(document.scripts)
                .map(script => script.textContent)
                .filter(text => /^\s*window\.__SSR_STATE__\s*=/.test(text));
            const rows = roots[0].querySelectorAll(":scope > div > [data-index]");
            const snippets = Array.from(rows).map(row => {
                const index = row.getAttribute("data-index");
                const cards = row.querySelectorAll("[data-e2e-id='snippet-item']");
                if (!/^[0-9]+$/.test(index) || cards.length !== 1) return null;
                const item = cards[0];
                const exact = selector => {
                    const found = item.querySelectorAll(selector);
                    return found.length === 1 && found[0].getClientRects().length
                        ? found[0].innerText : null;
                };
                const amount = exact("[data-e2e-id='short-rent-price']");
                const nightly = exact("[data-e2e-id='short-rent-price-per-night']");
                const links = Array.from(item.querySelectorAll("a[href]")).map(a => a.href);
                return {index: Number(index), amount, nightly, links};
            });
            return {dates: date[0].value, guests: guests[0].value, snippets, scripts};
        }"""
    )
    context = as_object(projection)
    dates, guests, snippets, scripts = (
        context.get("dates"),
        context.get("guests"),
        context.get("snippets"),
        context.get("scripts"),
    )
    if (
        not isinstance(dates, str)
        or not isinstance(guests, str)
        or not isinstance(snippets, list)
        or not isinstance(scripts, list)
    ):
        return None
    if response_ids is None:
        serp = as_object(ssr_state(cast(list[str], scripts)).get("serp"))
        search = as_object(serp.get("searchData"))
        ids = serp.get("ids")
        if (
            serp.get("fetchStatus") != "success"
            or serp.get("outdatedSearch") is not False
            or serp.get("invalidParams") is not False
            or not isinstance(ids, list)
            or search.get("adt") != 1
            or search.get("dateStart") not in (None, "", UNDEFINED)
            or search.get("dateEnd") not in (None, "", UNDEFINED)
        ):
            return None
        published_ids = tuple(cast(list[object], ids))
    else:
        published_ids = response_ids
    if len(snippets) > len(published_ids):
        return None

    # SerpPage renders each primary virtual row at data-index.
    quote = None
    rendered_indices: set[int] = set()
    for raw in cast(list[object], snippets):
        snippet = as_object(raw)
        index, links = snippet.get("index"), snippet.get("links")
        if (
            type(index) is not int
            or not 0 <= index < len(published_ids)
            or index in rendered_indices
            or not isinstance(links, list)
        ):
            return None
        rendered_indices.add(index)
        destinations = {
            destination: link
            for link in cast(list[object], links)
            if isinstance(link, str) and (destination := _identity(link)) is not None
        }
        if len(destinations) != 1:
            return None
        destination, link = next(iter(destinations.items()))
        if destination.family != "daily" or not _daily_id_matches(
            published_ids[index], destination
        ):
            return None
        if destination != identity:
            continue
        amount, nightly = snippet.get("amount"), snippet.get("nightly")
        if quote or not isinstance(amount, str) or not isinstance(nightly, str):
            return None
        quote = DailyListingQuote(link, dates, guests, amount, nightly)
    return quote


def _parse_native(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    data: dict[str, object],
    removed_marker: bool,
    page_title: str,
    observe: Observe | None,
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

    return observe_publication(
        observe,
        key,
        url,
        lambda: domclick_observations.native_fields(data, page_title),
        lambda: normalize_native_card(
            key,
            url,
            data,
            native_type=identity.native_type,
            deal=identity.deal,
            page_title=page_title,
        ),
    )


def _parse_partner(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    nuxt_scripts: list[str],
    observe: Observe | None,
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

    return observe_publication(
        observe,
        key,
        url,
        lambda: domclick_observations.partner_fields(state, detail),
        lambda: normalize_partner_card(key, url, state, detail, deal=identity.deal),
    )


def _parse_daily(
    key: SnapshotKey,
    identity: CardIdentity,
    url: str,
    source: dict[str, object],
    gallery: list[str],
    quote: DailyListingQuote | None,
    page_title: str,
    observe: Observe | None,
) -> Snapshot:
    inventory = as_object(source.get("inventory"))
    search = as_object(inventory.get("searchData"))
    card_id = search.get("id")
    data = as_object(inventory.get("data"))
    if (
        not _daily_id_matches(card_id, identity)
        or inventory.get("fetchStatus") != "success"
        or data.get("status") != "published"
    ):
        return {**key, "state": "unavailable"}
    if quote is not None and _identity(quote.card_url) != identity:
        quote = None
    return observe_publication(
        observe,
        key,
        url,
        lambda: domclick_observations.daily_fields(data, page_title, gallery, quote),
        lambda: normalize_daily_card(
            key,
            url,
            data,
            deal=identity.deal,
            gallery=gallery,
            quote=quote,
            page_title=page_title,
        ),
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
    observe: Observe | None = None,
) -> Snapshot:
    identity = _identity(requested_url)
    if identity is None or observed_at.utcoffset() is None:
        raise RejectedCard("invalid_identity")
    key: SnapshotKey = {
        "source_code": "domclick",
        "external_id": identity.key,
        "observed_at": observed_at,
    }
    if not same_card(requested_url, loaded_url):
        return {**key, "state": "unavailable"}
    if identity.family == "partner":
        return _parse_partner(key, identity, loaded_url, nuxt_scripts, observe)
    state = ssr_state(ssr_scripts)
    if identity.family == "daily":
        return _parse_daily(
            key, identity, loaded_url, state, gallery or [], quote, page_title, observe
        )
    if identity.family == "native":
        return _parse_native(
            key,
            identity,
            loaded_url,
            as_object(state.get("productCard")),
            removed_marker,
            page_title,
            observe,
        )
    shell = as_object(state.get("shell"))
    flat = as_object(as_object(shell.get("flatDetails")).get("data"))
    if shell.get("isDeletedFlat") is not False or not _number_matches(
        flat.get("id"), identity
    ):
        return {**key, "state": "unavailable"}
    geography = as_object(state.get("regions"))
    return observe_publication(
        observe,
        key,
        loaded_url,
        lambda: domclick_observations.native_fields(
            flat, page_title, geography=geography
        ),
        lambda: normalize_new_flat_card(key, loaded_url, flat, geography, page_title),
    )


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
    observe: Observe | None = None,
) -> Snapshot:
    """Read the public documents already loaded by the collection runner."""
    if _identity(requested_url) is None:
        raise RejectedCard("invalid_identity")
    if deadline is None:
        deadline = time.monotonic() + 30
    data = wait_for_card_data(
        page,
        r"""() => {
            if (document.readyState !== "complete") return false;
            const scripts = Array.from(document.scripts).filter(s => !s.src);
            const removed = "Объявление снято с размещения";
            const unrelated = "aside, nav, footer, [role='complementary']";
            const main = document.querySelectorAll("main, [role='main']");
            const marker = main.length === 1 && !main[0].closest(unrelated)
                && Array.from(main[0].querySelectorAll("*")).some(element =>
                    element.children.length === 0 && element.getClientRects().length
                    && !element.closest(unrelated)
                    && element.innerText?.replace(/\s+/g, " ").trim() === removed);
            return {
                ssr: scripts.map(s => s.textContent)
                    .filter(s => /^\s*window\.__SSR_STATE__\s*=/.test(s)),
                nuxt: scripts.filter(s => s.id === "__NUXT_DATA__")
                    .map(s => s.textContent),
                removed: marker,
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
    if projected["title"].strip() == "403 | Домклик":
        raise ChallengePending(Challenge("domclick", "click", "qauth"))
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
        observe=observe,
    )
