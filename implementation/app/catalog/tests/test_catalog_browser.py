"""Guest catalog acceptance on PostgreSQL and Chromium."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import DatabaseError
from django.test import Client
from playwright.sync_api import Page, expect, sync_playwright

from app.catalog.models import (
    Ad,
    DealType,
    MarketSegment,
    PriceHistory,
    PriceKind,
    Property,
    PropertyType,
    Region,
    Source,
)
from app.catalog.reference_defaults import (
    APARTMENT_TYPE_ID,
    HOUSE_TYPE_ID,
    PROPERTY_TYPES,
    ROOM_TYPE_ID,
    SAMARA_REGION_ID,
    SOURCE_CODES,
)

OBSERVED = datetime(2026, 11, 19, 10, tzinfo=UTC)
PRICE_RECORDED = datetime(2026, 11, 17, 10, tzinfo=UTC)
PHOTO_URL = "https://media.example.test/catalog-photo.jpg"


def _offer(
    item: Property,
    source: str,
    deal: DealType,
    price: int | None,
    *,
    segment: MarketSegment | None = None,
    photos: list[str] | None = None,
    price_kind: PriceKind = PriceKind.LISTED,
    active: bool = True,
) -> None:
    ad = Ad.objects.create(
        property=item,
        source_id=source,
        external_id=f"catalog-{item.pk}-{deal}-{int(active)}",
        url=f"https://example.test/{source}/{item.pk}/{deal}/",
        deal_type=deal,
        market_segment=segment,
        is_active=active,
        observed_at=OBSERVED,
        photos=photos or [],
        photos_observed_at=OBSERVED if photos else None,
    )
    if price is not None:
        PriceHistory.objects.create(
            ad=ad,
            observed_at=PRICE_RECORDED,
            price_rub=Decimal(price),
            price_kind=price_kind,
        )


def seed_catalog() -> None:
    Region.objects.get_or_create(
        pk=SAMARA_REGION_ID, defaults={"name": "Самарская область"}
    )
    for pk, name in PROPERTY_TYPES:
        PropertyType.objects.get_or_create(pk=pk, defaults={"name": name})
    for code in SOURCE_CODES:
        Source.objects.get_or_create(pk=code, defaults={"enabled": True})
    for number in range(1, 22):
        apartment = Property.objects.create(
            region_id=SAMARA_REGION_ID,
            property_type_id=APARTMENT_TYPE_ID,
            address=f"Тестовая улица, {number:02d}",
            area_m2=Decimal("70"),
            rooms=0 if number == 1 else 2,
            floor=0 if number == 1 else 3,
            floors_total=9,
        )
        _offer(
            apartment, "cian", DealType.SALE, 10_000_000, segment=MarketSegment.RESALE
        )
        if number == 1:
            _offer(apartment, "domclick", DealType.SALE, 9_000_000, photos=[PHOTO_URL])
            _offer(apartment, "avito", DealType.MONTH_RENT, 900)
            _offer(apartment, "avito", DealType.SALE, 700, active=False)
    room = Property.objects.create(
        region_id=SAMARA_REGION_ID,
        property_type_id=ROOM_TYPE_ID,
        address="Комнатная улица, 7",
        area_m2=Decimal("9.2"),
        rooms=1,
        floor=-1,
        floors_total=5,
    )
    _offer(room, "cian", DealType.DAY_RENT, 3000, price_kind=PriceKind.FROM)
    _offer(room, "domclick", DealType.MONTH_RENT, None)
    house = Property.objects.create(
        region_id=SAMARA_REGION_ID,
        property_type_id=HOUSE_TYPE_ID,
        address="Зелёная улица, 5",
        area_m2=Decimal("100"),
        land_area_m2=Decimal("624"),
        floors_total=1,
    )
    _offer(house, "avito", DealType.DAY_RENT, 4000)


def check_catalog(page: Page, live_server_url: str) -> None:
    # Arrange: exercise a broken public photo without leaving the local browser.
    page.route(PHOTO_URL, lambda route: route.abort())
    url = f"{live_server_url}/catalog/"
    cards = page.locator("article.result")
    listing_link = page.get_by_role("link", name="Вперёд")

    # Act: open the guest catalog and read the first group of results.
    response = page.goto(f"{live_server_url}/")

    # Assert: the first page groups qualifying offers and orders by property ID.
    assert response is not None and response.status == 200
    expect(page).to_have_url(url)
    expect(page.get_by_role("combobox", name="Сделка")).to_have_value("sale")
    expect(page.locator('#id_deal_type option[value="sale"]')).to_have_text("Продажа")
    expect(cards.first).to_contain_text("Вторичка")
    expect(cards).to_have_count(20)
    expect(cards.first).to_contain_text("Тестовая улица, 01")
    expect(cards.first).to_contain_text("Студия")
    expect(cards.first).to_contain_text("Этаж: 0")
    expect(cards.first.locator(".offers a")).to_have_count(2)
    expect(cards.first.get_by_text("Фото недоступно")).to_be_visible()
    expect(cards.first.get_by_text("Цена зафиксирована", exact=False)).to_have_count(0)
    expect(page.get_by_role("link", name="JSON")).to_have_count(0)

    # Act: apply filters, go to page two and inspect the context passed to the object link.
    page.get_by_role("combobox", name="Тип жилья").select_option("flat")
    expect(page).to_have_url(re.compile(r"[?&]category=flat"))
    page.get_by_role("button", name="Регион: Все регионы").click()
    region_search = page.get_by_role("searchbox", name="Найти регион")
    region_search.fill("самар")
    region_search.press("Enter")
    region = page.locator("#id_region_id")
    expect(page).to_have_url(re.compile(rf"[?&]region_id={SAMARA_REGION_ID}"))
    page.get_by_role("button", name="Ещё фильтры").click()
    page.locator("#id_area_min").fill("69,0")
    expect(page).to_have_url(re.compile(r"[?&]area_min=69%2C0"))
    listing_link.click()
    params = parse_qs(urlsplit(page.url).query)
    prop_url = cards.first.locator("a.object-link").get_attribute("href")

    # Assert
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("Тестовая улица, 21")
    assert params == {
        "deal_type": ["sale"],
        "category": ["flat"],
        "region_id": [str(SAMARA_REGION_ID)],
        "area_min": ["69.0"],
        "page": ["2"],
    }
    assert prop_url is not None
    assert parse_qs(urlsplit(prop_url).query) == {"from": ["catalog"], **params}

    # Act: reset, then ask for a sale with only the cheap rent or removed offer in range.
    page.get_by_role("link", name="Сбросить", exact=True).click()
    expect(page.get_by_role("combobox", name="Тип жилья")).to_have_value("all")
    expect(region).to_have_value("")
    expect(page.locator("#id_area_min")).to_have_value("")
    page.locator("#id_price_max").fill("1000")

    # Assert: deal, activity and price apply to the same offer.
    expect(
        page.get_by_role("heading", name="По выбранным фильтрам ничего не найдено")
    ).to_be_visible()
    expect(cards).to_have_count(0)

    # Act: a deal switch retains the region even when its original key was padded.
    page.goto(f"{url}?price_max=1000&region_id=%20000{SAMARA_REGION_ID}%20")
    expect(region).to_have_value(str(SAMARA_REGION_ID))
    page.get_by_role("combobox", name="Тип жилья").select_option("room")
    page.get_by_role("button", name="Ещё фильтры").click()
    page.locator("#id_floor_min").fill("-1")
    page.get_by_role("combobox", name="Сделка").select_option("day_rent")
    expect(page).to_have_url(re.compile(r"[?&]deal_type=day_rent"))

    # Assert
    params = parse_qs(urlsplit(page.url).query)
    assert params["category"] == ["room"] and params["region_id"] == [
        str(SAMARA_REGION_ID)
    ]
    assert params["floor_min"] == ["-1"] and not params.get("price_max")
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("от 3 000 руб./сут.")
    expect(cards.first).to_contain_text("Предлагаемых комнат: 1")
    expect(page.get_by_text("Фото не указано")).to_be_visible()

    # Act: inspect a missing monthly price, then try an impossible page.
    page.get_by_role("combobox", name="Тип жилья").select_option("all")
    page.get_by_role("combobox", name="Сделка").select_option("month_rent")
    expect(cards.first).to_contain_text("900 руб./мес.")
    expect(cards.last.get_by_text("Цена не получена")).to_be_visible()
    response = page.goto(f"{url}?deal_type=day_rent&category=house&page=2")

    # Assert: a missing page is a 400 and keeps the normalized search on retry.
    assert response is not None and response.status == 400
    expect(page.get_by_text("Такой страницы уже нет.", exact=False)).to_be_visible()
    page.get_by_role("link", name="На первую страницу").click()
    expect(cards.first).to_contain_text("Участок: 624 м²")
    expect(cards.first).to_contain_text("Этажность: 1")


class CatalogBrowserTests(StaticLiveServerTestCase):
    def test_guest_search_links_prices_and_photo_fallback(self) -> None:
        # Arrange
        seed_catalog()

        # Act / Assert
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            check_catalog(context.new_page(), self.live_server_url)

    def test_shortlist_keeps_deals_separate_and_compares_published_sale_prices(
        self,
    ) -> None:
        # Arrange
        seed_catalog()
        first, second = Property.objects.order_by("pk")[:2]
        client = Client(enforce_csrf_checks=True)
        search = f"/catalog/?deal_type=sale&page=2#favorite-sale-{first.pk}"
        change = {
            "action": "save",
            "deal": "sale",
            "property_id": first.pk,
            "return_to": search,
        }

        # Act
        unprotected = client.post("/favorites/", change)
        client.get("/catalog/")
        headers = {"X-CSRFToken": client.cookies["csrftoken"].value}
        saved = client.post("/favorites/", change, headers=headers)
        client.post("/favorites/", {**change, "deal": "month_rent"}, headers=headers)
        client.post(
            "/favorites/", {**change, "property_id": second.pk}, headers=headers
        )
        query = urlencode(
            [("deal", "sale"), ("property_id", first.pk), ("property_id", second.pk)]
        )
        comparison = client.get(f"/compare/?{query}")
        client.post("/favorites/", {**change, "action": "remove"}, headers=headers)

        # Assert
        self.assertEqual(unprotected.status_code, 403)
        self.assertRedirects(saved, search, fetch_redirect_response=False)
        self.assertContains(comparison, "9\u00a0000\u00a0000 руб.")
        self.assertNotContains(comparison, "руб./мес.")
        self.assertEqual(
            comparison.context["recommendation"]["value"], Decimal("9000000")
        )
        sales = client.get("/favorites/?deal=sale")
        monthly = client.get("/favorites/?deal=month_rent")
        self.assertContains(sales, f'id="compare-sale-{second.pk}"')
        self.assertNotContains(sales, f'id="compare-month_rent-{first.pk}"')
        self.assertContains(monthly, f'id="compare-month_rent-{first.pk}"')
        self.assertNotContains(monthly, f'id="compare-sale-{second.pk}"')
        self.assertContains(monthly, "Тестовая улица, 01", count=1)
        self.assertContains(Client().get("/favorites/"), "Избранное пока пусто")

    def test_comparison_session_keeps_budget_and_separates_shared_links(self) -> None:
        # Arrange
        seed_catalog()
        first, second, third = Property.objects.order_by("pk")[:3]
        client = Client(enforce_csrf_checks=True)
        empty = client.get("/compare/")
        guest_session = client.cookies["sessionid"].value
        client.get("/catalog/")
        headers = {"X-CSRFToken": client.cookies["csrftoken"].value}
        change = {"deal": DealType.SALE, "action": "save", "property_id": first.pk}

        # Act
        rejected = client.post("/compare/", change)
        client.post("/compare/", change, headers=headers)
        single = client.get("/compare/")
        client.post("/compare/", {**change, "property_id": second.pk}, headers=headers)
        client.post("/compare/", {**change, "property_id": third.pk}, headers=headers)
        client.post(
            "/compare/",
            {
                "action": "configure",
                "deal": DealType.SALE,
                "budget": "9500000",
                "set_pair": f"{first.pk},{second.pk}",
                "set_differences": "1",
            },
            headers=headers,
        )
        client.get("/compare/?deal=month_rent")
        comparison = client.get("/compare/?deal=sale")
        shared_query = urlencode(
            [
                ("deal", DealType.SALE),
                ("property_id", first.pk),
                ("property_id", second.pk),
                ("property_id", third.pk),
                ("pair", f"{first.pk},{second.pk}"),
            ]
        )
        guest = Client()
        shared = guest.get(f"/compare/?{shared_query}")
        guest_selection = guest.get("/compare/")
        client.post(
            "/compare/",
            {"action": "configure", "deal": DealType.SALE, "budget": "13000000"},
            headers=headers,
        )
        after_stale_toggle = client.post(
            "/compare/",
            {
                "action": "configure",
                "deal": DealType.SALE,
                "budget": "9500000",
                "set_differences": "0",
            },
            headers=headers,
            follow=True,
        )

        # Assert
        self.assertContains(empty, "Добавьте два объекта")
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(client.cookies["sessionid"].value, guest_session)
        self.assertContains(single, "Выбрать второй")
        self.assertContains(comparison, 'data-columns="2"')
        self.assertContains(comparison, "data-mark-differences")
        self.assertEqual(
            comparison.context["recommendation"]["value"], Decimal("9000000")
        )
        self.assertEqual(
            comparison.context["counts"], {"within": 1, "over": 1, "unknown": 0}
        )
        self.assertTrue(comparison.context["rows"]["area"]["visible"])
        self.assertFalse(comparison.context["rows"]["area"]["different"])
        self.assertContains(shared, "Сравнение по ссылке")
        self.assertContains(shared, 'data-columns="2"')
        self.assertContains(guest_selection, "Добавьте два объекта")
        self.assertContains(after_stale_toggle, 'data-columns="2"')
        self.assertNotContains(after_stale_toggle, "data-mark-differences")
        self.assertEqual(after_stale_toggle.context["budget"], Decimal("13000000"))

    def test_market_counts_offers_and_uses_only_latest_exact_prices(self) -> None:
        # Arrange
        seed_catalog()

        # Act
        sales = self.client.get("/market/?deal_type=sale&category=flat")
        daily = self.client.get("/market/?deal_type=day_rent&category=room")
        empty = self.client.get("/market/?deal_type=month_rent&category=house")

        # Assert
        self.assertEqual(sales.context["summary"]["total"], 22)
        self.assertEqual(sales.context["summary"]["objects"], 21)
        self.assertEqual(sales.context["summary"]["median_price"], Decimal("10000000"))
        self.assertEqual(daily.context["summary"]["lower_bounds"], 1)
        self.assertIsNone(daily.context["summary"]["median_price"])
        self.assertContains(empty, "Объявлений в этом срезе пока нет")

    def test_map_uses_published_coordinates_and_preserves_area_on_return(self) -> None:
        # Arrange
        seed_catalog()
        first, second = Property.objects.order_by("pk")[:2]
        Property.objects.filter(pk=first.pk).update(
            latitude=Decimal("53.2"), longitude=Decimal("50.1")
        )
        Property.objects.filter(pk=second.pk).update(
            latitude=Decimal("53.6"), longitude=Decimal("51")
        )

        # Act
        path = "/map/?deal_type=sale&bbox=53.1,50.0,53.3,50.2"
        selected = self.client.get(path)
        points = selected.context["state"]["points"]
        card = self.client.get(points[0]["url"])
        invalid = self.client.get("/map/?bbox=54,50,53,51")

        # Assert
        self.assertEqual(selected.context["total"], 1)
        self.assertEqual(selected.context["missing"], 19)
        self.assertEqual(
            [(point["lat"], point["lon"]) for point in points], [(53.2, 50.1)]
        )
        self.assertEqual(points[0]["amount"], "9000000.00")
        self.assertEqual(card.context["back_url"], path)
        self.assertEqual(invalid.status_code, 400)

    def test_empty_catalog_and_server_error_are_distinct(self) -> None:
        # Arrange
        self.client.raise_request_exception = False

        # Act
        empty = self.client.get("/catalog/")
        with patch(
            "app.catalog.views.read_catalog",
            side_effect=DatabaseError("private connection details"),
        ):
            failed = self.client.get("/catalog/")

        # Assert
        self.assertContains(empty, "Данных пока нет")
        self.assertContains(failed, "Не удалось загрузить данные", status_code=500)
        self.assertNotContains(failed, "private connection details", status_code=500)
