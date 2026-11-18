"""Read Avito's confirmed 6.1 rental, sale and closed-card formats."""

import json
import re
import time
from datetime import datetime
from decimal import Decimal
from html import unescape
from typing import Literal, cast
from unicodedata import normalize
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from catalog.contracts import (
    ActiveSnapshot,
    Deal,
    KnownPhotos,
    KnownPrice,
    Snapshot,
    SnapshotKey,
    Unavailable,
    is_database_text,
    is_http_url,
)
from collection.browser import NAVIGATION_TIMEOUT_MS
from collection.normalization import (
    RejectedCard,
    as_object,
    coordinate,
    decimal_2,
    number,
    optional_integer,
)

_CARD_PATH = re.compile(
    r"/[a-z0-9_-]+/(kvartiry|komnaty|doma_dachi_kottedzhi)"
    r"/[a-z0-9_.-]+_([1-9][0-9]{0,19})/?"
)
_ORIGIN = "https://www.avito.ru"
_FLAT, _ROOM, _HOUSE = "kvartiry", "komnaty", "doma_dachi_kottedzhi"
_TYPES = {_FLAT: 1, _ROOM: 2, _HOUSE: 3}
_LABELS = {
    "Количество комнат",
    "Общая площадь",
    "Площадь комнаты",
    "Этаж",
    "Площадь дома",
    "Площадь участка",
    "Этажей в доме",
    "Условия продажи",
}
_ABBREVIATIONS = {
    "обл.": "область",
    "ул.": "улица",
    "р-н": "район",
    "стр.": "строение",
    "пр-т": "проспект",
    "просп.": "проспект",
    "пер.": "переулок",
    "пос.": "посёлок",
    "кв.": "квартира",
    "корп.": "корпус",
    "под.": "подъезд",
}
_ADDRESS_ABBREVIATION = re.compile(
    r"(?<!\S)(" + "|".join(map(re.escape, _ABBREVIATIONS)) + r")(?=\s|,|$)",
    re.IGNORECASE,
)
_DAILY_LIST_PRICE = re.compile(r"(от )?([0-9][0-9 ,.]*) ?₽ за (?:1 )?сутки")
_AREA = re.compile(r"(.+?)\s+(м²|сот\.|га)")


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


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _params(buyer: dict[str, object]) -> dict[str, str]:
    values = as_object(buyer.get("paramsDto")).get("items")
    if not isinstance(values, list):
        raise RejectedCard("invalid_card_params")
    result: dict[str, str] = {}
    for raw in cast(list[object], values):
        param = as_object(raw)
        label = _text(param.get("title"))
        if label not in _LABELS:
            continue
        value = _text(param.get("description"))
        if not value or label in result:
            raise RejectedCard("invalid_card_param")
        result[label] = value
    return result


def _area(value: str | None, *, land: bool = False) -> Decimal | None:
    if value is None:
        return None
    match = _AREA.fullmatch(value)
    if match is None or (not land and match[2] != "м²"):
        raise RejectedCard("invalid_area_unit")
    multiplier = {"м²": 1, "сот.": 100, "га": 10000}[match[2]]
    return decimal_2(
        number(match[1]) * multiplier,
        minimum=Decimal("0.01"),
        maximum=Decimal("1e12" if land else "1e10"),
    )


def _address(item: dict[str, object]) -> tuple[str, int]:
    raw = item.get("address")
    if not is_database_text(raw) or not raw.strip():
        raise RejectedCard("invalid_address")
    address = _ADDRESS_ABBREVIATION.sub(
        lambda match: _ABBREVIATIONS[match[0].casefold()],
        normalize("NFC", _text(raw)),
    )
    subject = address.split(",", 1)[0].casefold()
    location = as_object(item.get("location"))
    if subject not in ("самарская область", "самара") and not (
        location.get("slug") == "samara"
        and item.get("locationId") == 653040
        and subject.startswith("улица ")
    ):
        raise RejectedCard("unconfirmed_avito_region")
    return address, 68


def _point(item: dict[str, object]) -> tuple[Decimal | None, Decimal | None]:
    geo = item.get("geo")
    if geo is not None and not isinstance(geo, dict):
        raise RejectedCard("invalid_geo")
    coords = as_object(geo).get("coords")
    if coords is None:
        return None, None
    result = as_object(coords)
    return coordinate(result.get("lat"), 90), coordinate(result.get("lng"), 180)


def _photos(item: dict[str, object]) -> KnownPhotos | Unavailable:
    gallery = item.get("imageUrls")
    if not isinstance(gallery, list):
        return {"status": "unavailable"}
    urls = [as_object(row).get("1280x960") for row in cast(list[object], gallery)]
    if not all(is_http_url(url) for url in urls):
        return {"status": "unavailable"}
    return {"status": "known", "urls": list(dict.fromkeys(cast(list[str], urls)))}


def _price(
    item: dict[str, object],
    identity: tuple[str, str],
    deal: Deal,
    listing_price: tuple[str, str] | None,
) -> KnownPrice | Unavailable:
    formatted = as_object(item.get("formattedPrice"))
    if formatted.get("currencySign") != "₽" or formatted.get("isHasValue") is not True:
        return {"status": "unavailable"}
    display = formatted.get("formatedString")
    if not isinstance(display, str) or not display.strip():
        return {"status": "unavailable"}
    try:
        bounds = {"minimum": Decimal(0), "maximum": Decimal("1e14")}
        amount = decimal_2(item.get("price"), **bounds)
        if decimal_2(formatted.get("value"), **bounds) != amount:
            return {"status": "unavailable"}
        kind: Literal["from", "listed"] = "listed"
        if _text(unescape(display)).casefold().split()[:1] == ["от"]:
            kind = "from"
        elif deal == "day_rent":
            if listing_price is None or _identity(listing_price[0]) != identity:
                return {"status": "unavailable"}
            listed = _DAILY_LIST_PRICE.fullmatch(_text(listing_price[1]).casefold())
            if not listed or decimal_2(listed[2], **bounds) != amount:
                return {"status": "unavailable"}
            if listed[1]:
                kind = "from"
    except RejectedCard:
        return {"status": "unavailable"}
    return {
        "status": "known",
        "price_rub": amount,
        "price_kind": kind,
        "currency": "RUB",
    }


def _classify(
    family: str,
    item: dict[str, object],
    buyer: dict[str, object],
    params: dict[str, str],
    title: str,
) -> tuple[Deal, Literal["new_build", "resale"] | None]:
    events = buyer.get("ga")
    classification = (
        as_object(events[1]) if isinstance(events, list) and len(events) > 1 else {}
    )
    offer = classification.get("offer_type")
    item_title = _text(item.get("title")).casefold()
    page_title = _text(title).casefold()
    formatted = as_object(item.get("formattedPrice"))
    postfix = _text(formatted.get("postfix"))
    if family == _FLAT:
        if offer == "Продам" and classification.get("status") == "Квартира":
            if postfix:
                raise RejectedCard("conflicting_card_deal")
            if classification.get("type") == "Новостройка":
                return "sale", "new_build"
            if classification.get("type") == "Вторичка":
                return "sale", "resale"
            raise RejectedCard("unconfirmed_market_segment")
        if offer == "Сдам" and (
            re.search(r"\bквартира\b", item_title)
            or re.search(r"\bквартира\b", page_title.split("|", 1)[0])
        ):
            if postfix == "в месяц":
                return "month_rent", None
            if postfix == "за сутки":
                return "day_rent", None
            raise RejectedCard("unconfirmed_rent_period")
    if not postfix and offer in (None, "Продам"):
        if family == _ROOM and "Площадь комнаты" in params:
            if params.get("Условия продажи") == "Продажа доли":
                if not item_title.startswith("доля: комната "):
                    raise RejectedCard("unconfirmed_room_unit")
                return "sale", None
            if item_title.startswith("комната ") and (
                offer == "Продам" or "на продажу" in page_title
            ):
                return "sale", None
        if (
            family == _HOUSE
            and item_title.startswith("дом ")
            and "Площадь дома" in params
            and (offer == "Продам" or "на продажу" in page_title)
        ):
            return "sale", None
    raise RejectedCard("unconfirmed_avito_card_family")


def _active(
    identity: tuple[str, str],
    key: SnapshotKey,
    url: str,
    buyer: dict[str, object],
    title: str,
    expected_deal: Deal | None,
    listing_price: tuple[str, str] | None,
) -> ActiveSnapshot:
    family = identity[0]
    item = as_object(buyer["item"])
    params = _params(buyer)
    deal, segment = _classify(family, item, buyer, params, title)
    if expected_deal is not None and expected_deal != deal:
        raise RejectedCard("unexpected_card_deal")
    address, region = _address(item)
    lat, lon = _point(item)
    area_label = {
        _FLAT: "Общая площадь",
        _ROOM: "Площадь комнаты",
        _HOUSE: "Площадь дома",
    }
    area = _area(params.get(area_label[family]))
    floor = None
    floors = None
    if family == _HOUSE:
        floors = optional_integer(params.get("Этажей в доме"), minimum=1)
    elif (floor_value := params.get("Этаж")) is not None:
        pair = floor_value.split(" из ")
        if len(pair) != 2:
            raise RejectedCard("invalid_floor_pair")
        floor = optional_integer(pair[0], minimum=-32768)
        floors = optional_integer(pair[1], minimum=1)
        if floor is not None and floors is not None and floor > floors:
            raise RejectedCard("floor_above_building")
    room_count = params.get("Количество комнат")
    rooms = (
        (
            0
            if family == _FLAT and room_count == "Студия"
            else optional_integer(room_count, minimum=1)
        )
        if family != _ROOM
        else None
    )
    description = item.get("description")
    if description is not None and not is_database_text(description):
        raise RejectedCard("invalid_description")
    return {
        **key,
        "state": "active",
        "url": url,
        "region_id": region,
        "property_type_id": _TYPES[family],
        "deal_type": deal,
        "market_segment": segment,
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "area_m2": area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors,
        "land_area_m2": _area(params.get("Площадь участка"), land=True)
        if family == _HOUSE
        else None,
        "description": description or None,
        "price": _price(item, identity, deal, listing_price),
        "photos": _photos(item),
    }


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
    return _active(
        identity, key, loaded_url, buyer, page_title, expected_deal, listing_price
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
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(
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
            const project = (value, fields) => value && typeof value === "object"
                ? Object.fromEntries(fields.map(field => [field, value[field]]))
                : value;
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
        arg=requested_url,
        timeout=remaining_ms,
    )
    try:
        serialized = cast(str, handle.json_value())
    finally:
        handle.dispose()
    if time.monotonic() >= deadline:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    data = as_object(json.loads(serialized, parse_float=Decimal))
    return parse_card(
        requested_url,
        observed_at,
        as_object(data.get("route")),
        loaded_url=cast(str, data["url"]),
        page_title=cast(str, data["title"]),
        expected_deal=expected_deal,
        listing_price=listing_price,
    )
