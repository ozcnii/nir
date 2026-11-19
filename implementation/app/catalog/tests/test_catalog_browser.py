"""Guest catalog acceptance on PostgreSQL and Chromium."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import DatabaseError
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
    expect(cards.first.get_by_role("link", name="На площадку")).to_have_count(2)
    expect(cards.first.get_by_text("Фото недоступно")).to_be_visible()
    expect(
        cards.first.get_by_text("Последняя известная цена.", exact=False).first
    ).to_contain_text("17.11.2026")
    assert page.get_by_role("link", name="JSON").get_attribute("href") == (
        "/api/properties/?deal_type=sale&category=all&page=1"
    )

    # Act: apply filters, go to page two and inspect the context passed to the object link.
    page.get_by_role("combobox", name="Категория").select_option("flat")
    page.get_by_role("combobox", name="Регион").select_option(str(SAMARA_REGION_ID))
    page.locator("#id_area_min").fill("69,0")
    page.get_by_role("button", name="Найти").click()
    listing_link.click()
    params = parse_qs(urlsplit(page.url).query)
    prop_url = cards.first.get_by_role("link", name="Открыть объект").get_attribute(
        "href"
    )

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
    expect(page.get_by_role("combobox", name="Категория")).to_have_value("all")
    expect(page.get_by_role("combobox", name="Регион")).to_have_value("")
    expect(page.locator("#id_area_min")).to_have_value("")
    page.locator("#id_price_max").fill("1000")
    page.get_by_role("button", name="Найти").click()

    # Assert: deal, activity and price apply to the same offer.
    expect(
        page.get_by_role("heading", name="По выбранным фильтрам ничего не найдено")
    ).to_be_visible()
    expect(cards).to_have_count(0)

    # Act: a deal switch retains the region even when its original key was padded.
    page.goto(f"{url}?price_max=1000&region_id=%20000{SAMARA_REGION_ID}%20")
    expect(page.get_by_role("combobox", name="Регион")).to_have_value(
        str(SAMARA_REGION_ID)
    )
    page.get_by_role("combobox", name="Категория").select_option("room")
    page.locator("#id_floor_min").fill("-1")
    page.get_by_role("combobox", name="Сделка").select_option("day_rent")
    page.wait_for_url(f"{url}?deal_type=day_rent&*")

    # Assert
    params = parse_qs(urlsplit(page.url).query)
    assert params["category"] == ["room"] and params["region_id"] == [
        str(SAMARA_REGION_ID)
    ]
    assert params["floor_min"] == ["-1"] and "price_max" not in params
    expect(cards).to_have_count(1)
    expect(cards.first).to_contain_text("от 3 000 руб./сут.")
    expect(cards.first).to_contain_text("Предлагаемых комнат: 1")
    expect(page.get_by_text("Фото не указано")).to_be_visible()

    # Act: inspect a missing monthly price, then try an impossible page.
    page.get_by_role("combobox", name="Категория").select_option("all")
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

    def test_empty_catalog_and_server_error_are_distinct(self) -> None:
        # Arrange
        self.client.raise_request_exception = False

        # Act
        empty = self.client.get("/catalog/")
        with patch(
            "app.catalog.views.catalog_page",
            side_effect=DatabaseError("private connection details"),
        ):
            failed = self.client.get("/catalog/")

        # Assert
        self.assertContains(empty, "Данных пока нет")
        self.assertContains(failed, "Не удалось загрузить данные", status_code=500)
        self.assertNotContains(failed, "private connection details", status_code=500)
