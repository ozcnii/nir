"""Run the regional collect command through Chromium and app-role PostgreSQL."""

import json
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.management import call_command
from playwright.sync_api import Page, Route

from app.catalog.reference_defaults import APARTMENT_TYPE_ID, SAMARA_REGION_ID
from app.collection.browser import browser_page
from app.collection.collectors.cian import parse_card
from app.collection.normalization import as_object
from app.collection.tests.cian_fixture import projection, projection_html, script

HOST = "https://samara.cian.ru"
REGIONAL_URL = f"{HOST}/kupit-kvartiru-samarskaya-oblast/"
FIRST = 930631201
THIRD = 930631203
REMOVED = 930631204
MISSING = 930631205
RECOMMENDED = 930631206
FOREIGN = 930631207


def card(offer_id: int) -> tuple[str, dict[str, object], dict[str, object]]:
    url, offer, product = projection()
    url = url.replace(str(offer["id"]), str(offer_id))
    if offer_id == THIRD:
        url = url.replace("samara.cian", "tolyatti.cian")
        geo = as_object(offer["geo"])
        geo["address"] = [
            {
                "id": 4608,
                "name": "Самарская",
                "fullName": "Самарская область",
                "type": "location",
                "locationTypeId": 2,
            },
            {
                "name": "Тольятти",
                "fullName": "Тольятти",
                "type": "location",
                "locationTypeId": 1,
            },
        ]
        offer["geo"] = geo
    if offer_id == FOREIGN:
        offer["geo"] = {
            "address": [
                {
                    "id": 4609,
                    "name": "Саратовская",
                    "fullName": "Саратовская область",
                    "type": "location",
                    "locationTypeId": 2,
                },
            ],
        }
    return (
        url,
        {**offer, "id": offer_id, "cianId": offer_id},
        {
            **product,
            "sku": offer_id,
            "offers": {**as_object(product["offers"]), "url": url},
        },
    )


def listing(page_number: int, *, mode: str) -> str:
    ids = [FIRST, FOREIGN] if page_number < 3 else [THIRD]
    region = 4966 if mode == "drift" and page_number == 2 else 4608
    query: dict[str, object] = {
        "_type": "flatsale",
        "engine_version": {"type": "term", "value": 2},
        "region": {"type": "terms", "value": [region]},
    }
    if page_number > 1:
        query["page"] = {"type": "term", "value": page_number}
    result = {
        "jsonQuery": query,
        "offers": [{"id": offer_id} for offer_id in ids],
        "totalOffers": 3,
        "suggestions": [{"id": RECOMMENDED}],
    }
    config = json.dumps([{"key": "defaultState", "value": {"results": result}}])
    links = "".join(f'<a href="{card(key)[0]}">Объявление</a>' for key in ids)
    follow = "<button disabled>Дальше</button>"
    if page_number < 3:
        following = 1 if mode == "cycle" and page_number == 2 else page_number + 1
        path = f"/cat.php?deal_type=sale&engine_version=2&offer_type=flat&p={following}&region={region}"
        follow = f'<a href="{escape(path)}">Дальше</a>'
    state_script = f"window._cianConfig = {{'frontend-serp': {config}}};"
    if mode == "late-state" and page_number == 1:
        state_script = (
            "window.addEventListener('load', () => "
            f"setTimeout(() => {{ {state_script} }}, 700));"
        )
    return (
        "<title>Продажа квартир в Самарской области</title>"
        f"<script>{state_script}</script>"
        f'{links}{follow}<aside><a href="{card(RECOMMENDED)[0]}">Похожие</a></aside>'
    )


def collect_region(stdout: StringIO) -> None:
    call_command(
        "collect",
        "--source",
        "cian",
        "--region",
        str(SAMARA_REGION_ID),
        "--property-type",
        "apartment",
        "--deal",
        "sale",
        stdout=stdout,
    )


def verify_cian_search() -> None:
    from app.catalog.models import Ad, PriceHistory
    from app.catalog.publications import accept_snapshot

    # Arrange: seed two scoped known cards and one other-region card.
    for key in (REMOVED, MISSING):
        url, offer, product = card(key)
        snapshot = parse_card(
            url,
            datetime.now(UTC),
            [script(offer)],
            [json.dumps(product)],
            loaded_url=url,
        )
        assert accept_snapshot(snapshot).outcome == "created"
    missing = Ad.objects.get(source_id="cian", external_id=str(MISSING))
    missing_at = missing.observed_at
    mode = "late-state"
    requests: list[str] = []

    def serve(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        parts = urlsplit(url)
        status = 200
        if url == REGIONAL_URL or parts.path == "/cat.php":
            page = int(parse_qs(parts.query).get("p", ["1"])[0])
            body = listing(page, mode=mode)
        elif parts.path.startswith("/sale/flat/"):
            key = int(parts.path.rstrip("/").rsplit("/", 1)[1])
            _, offer, product = card(key)
            if key == REMOVED:
                body = f"<script>{script({'status': 'deleted'})}</script>"
            elif key not in (FIRST, THIRD, FOREIGN):
                status, body = 404, "Unavailable"
            elif key == FIRST and requests.count(url) == 1:
                status, body = 503, "Temporary"
                if mode == "stop":
                    signal.raise_signal(signal.SIGTERM)
            else:
                if mode == "replay" and key == FIRST:
                    product.pop("image")
                body = projection_html(offer, product)
        else:
            route.abort()
            return
        route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)

    @contextmanager
    def routed_browser() -> Iterator[Page]:
        with browser_page() as page:
            page.context.route("**/*", serve)
            yield page

    # Act
    with patch("app.collection.search.browser_page", routed_browser):
        completed = StringIO()
        collect_region(completed)

    # Assert
    summary = json.loads(completed.getvalue())
    assert summary["source"] == "cian"
    assert summary["scope"] == {
        "region_id": SAMARA_REGION_ID,
        "property_type_id": APARTMENT_TYPE_ID,
        "deal_type": "sale",
        "market_segment": None,
    }
    assert summary["listing_complete"] is True and summary["pages"] == 3, (
        summary,
        requests,
    )
    assert summary["issues"]["card_outside_scope"] == 1
    assert summary["issues"]["http_404"] >= 1
    assert summary["issues_by_stage"]["card"]["card_outside_scope"] == 1
    assert summary["issues_by_stage"]["known_card"]["http_404"] >= 1
    first = Ad.objects.get(source_id="cian", external_id=str(FIRST))
    third = Ad.objects.get(source_id="cian", external_id=str(THIRD))
    assert requests.count(card(FIRST)[0]) == 2
    assert requests.count(card(THIRD)[0]) == 1
    assert card(RECOMMENDED)[0] not in requests
    assert not Ad.objects.filter(source_id="cian", external_id=str(FOREIGN)).exists()
    assert "Тольятти" in (third.property.address or "")
    assert third.property.region_id == SAMARA_REGION_ID
    assert not Ad.objects.get(source_id="cian", external_id=str(REMOVED)).is_active
    missing.refresh_from_db()
    assert missing.is_active and missing.observed_at == missing_at
    assert list(
        PriceHistory.objects.filter(ad=first).values_list("price_rub", flat=True)
    ) == [
        Decimal("7246735.00"),
    ]

    # Arrange / Act
    mode = "replay"
    requests.clear()
    original_photo_time = first.photos_observed_at
    original_id = first.pk
    with patch("app.collection.search.browser_page", routed_browser):
        collect_region(StringIO())

    # Assert
    first.refresh_from_db()
    assert first.pk == original_id and first.photos
    assert first.photos_observed_at == original_photo_time
    assert PriceHistory.objects.filter(ad=first).count() == 1

    for case in ("cycle", "drift", "stop"):
        # Arrange
        mode = case
        requests.clear()
        output = StringIO()

        # Act
        with patch("app.collection.search.browser_page", routed_browser):
            collect_region(output)

        # Assert
        assert card(MISSING)[0] not in requests
        if case == "stop":
            assert requests == [REGIONAL_URL, card(FIRST)[0]]
            assert "Collection stopped." in output.getvalue()
        else:
            stopped = json.loads(output.getvalue())
            reason = "pagination_cycle" if case == "cycle" else "listing_scope_changed"
            assert stopped["listing_complete"] is False and reason in stopped["issues"]
            assert stopped["issues_by_stage"]["listing"] == {reason: 1}

    print(
        "Cian scope: Chromium waited for late state; pagination, known keys and stop verified"
    )
