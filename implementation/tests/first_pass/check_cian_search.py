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

from app.catalog.reference_defaults import SAMARA_REGION_ID
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
    assert summary["listing_complete"] is True and summary["pages"] == 3, (
        summary,
        requests,
    )
    assert summary["issues"]["card_outside_scope"] == 1
    assert summary["issues"]["http_404"] >= 1
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

    print(
        "Cian scope: Chromium waited for late state; pagination, known keys and stop verified"
    )


def verify_cian_daily_scope_drift() -> None:
    from app.catalog.models import Ad, PriceHistory

    # Arrange: replay the observed case of a broader list at the same URL.
    regional = f"{HOST}/snyat-komnatu-posutochno-samarskaya-oblast/"
    first_card = f"{HOST}/rent/flat/331646643/"
    foreign_ids = (325511826, 300832086)
    known = Ad.objects.get(source_id="cian", external_id="295679032")
    observed_at = known.observed_at
    prices = PriceHistory.objects.filter(ad=known)
    price_ids = list(prices.values_list("pk", flat=True))
    requests: list[str] = []
    base_query = {
        "_type": "flatrent",
        "engine_version": {"type": "term", "value": 2},
        "for_day": {"type": "term", "value": "1"},
    }
    pages = json.dumps(
        [
            {
                "query": {
                    **base_query,
                    "region": {"type": "terms", "value": [4608]},
                    "room": {"type": "terms", "value": [0]},
                },
                "list": {"page": 1, "status": "success", "hasMore": True, "current": 1},
                "offers": [{"id": 331646643}],
            },
            {
                "query": base_query,
                "list": {"page": 2, "status": "success", "hasMore": True, "current": 2},
                "offers": [{"id": key} for key in foreign_ids],
            },
        ]
    )
    foreign_links = "".join(
        f'<a href="/rent/flat/{key}/">Квартира</a>' for key in foreign_ids
    )
    html = f"""
      <h1>Снять посуточно комнату в Самарской области</h1>
      <main><a href="{first_card}">Комната</a></main><button>Дальше</button>
      <script>
        const pages = {pages};
        window._cianConfig = {{"frontend-serp":[{{key:"defaultState",value:pages[0]}}]}};
        document.querySelector("button").onclick = () => {{
          document.querySelector("h1").textContent = "Снять посуточно квартиру в России";
          document.querySelector("main").innerHTML = {json.dumps(foreign_links)};
          window._cianConfig["frontend-serp"][0].value = pages[1];
        }};
      </script>
    """

    def respond(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        route.fulfill(
            status=200 if url == regional else 404,
            content_type="text/html; charset=utf-8",
            body=html if url == regional else "Unavailable",
        )

    @contextmanager
    def routed_browser() -> Iterator[Page]:
        with browser_page() as page:
            page.context.route("**/*", respond)
            yield page

    # Act
    output = StringIO()
    with patch("app.collection.search.browser_page", routed_browser):
        call_command(
            "collect",
            "--source",
            "cian",
            "--region",
            str(SAMARA_REGION_ID),
            "--property-type",
            "room",
            "--deal",
            "day_rent",
            stdout=output,
        )

    # Assert: the second page and stored known cards stay outside this run's writes.
    summary = json.loads(output.getvalue())
    assert summary["listing_complete"] is False and summary["pages"] == 1
    assert summary["issues"] == {"http_404": 1, "listing_scope_changed": 1}
    assert requests == [regional, first_card], requests
    assert not Ad.objects.filter(
        source_id="cian", external_id__in=map(str, foreign_ids)
    ).exists()
    known.refresh_from_db()
    assert known.is_active and known.observed_at == observed_at
    assert list(prices.values_list("pk", flat=True)) == price_ids
    print("Cian daily: changed filters at the same URL did not write to PostgreSQL")
