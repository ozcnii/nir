"""Exercise public property detail, search context and database snapshot boundaries."""

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import DatabaseError, connection, models, transaction
from django.test import TransactionTestCase
from playwright.sync_api import Page, Route, expect, sync_playwright

from app.catalog.catalog_filters import BIGINT_MAX
from app.catalog.models import Ad, DealType, PriceHistory, PriceKind, Property
from app.catalog.property_details import property_details
from app.catalog.tests.test_catalog_browser import (
    OBSERVED,
    PRICE_RECORDED,
    seed_catalog,
)

BROKEN_PHOTO = "https://media.example.test/card/missing.jpg"
PUBLISHED_PHOTO = "https://media.example.test/card/published.png"
REMOVED_PHOTO = "https://media.example.test/card/archived.png"
PUBLISHED_PIXEL = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m"
    "Nk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def seed_property_card() -> tuple[int, int]:
    seed_catalog()
    item = Property.objects.get(address="Тестовая улица, 21")
    sale = Ad.objects.get(property=item)
    sale.external_id = "listing-sale"
    sale.description = "Первый абзац владельца.\n\nВторой абзац владельца."
    sale.photos = [BROKEN_PHOTO, PUBLISHED_PHOTO]
    sale.photos_observed_at = OBSERVED
    sale.save()
    price = PriceHistory.objects.get(ad=sale)
    price.price_kind = PriceKind.FROM
    price.save()
    PriceHistory.objects.create(
        ad=sale,
        observed_at=PRICE_RECORDED + timedelta(days=1),
        price_rub=price.price_rub,
        price_kind=PriceKind.LISTED,
    )
    Ad.objects.create(
        property=item,
        source_id="cian",
        external_id="listing-month",
        url="https://example.test/cian/month/",
        deal_type=DealType.MONTH_RENT,
        is_active=True,
        photos_observed_at=OBSERVED,
        observed_at=OBSERVED,
    )
    rent = Ad.objects.create(
        property=item,
        source_id="avito",
        external_id="listing-day",
        url="https://example.test/avito/daily/",
        description="Посуточное предложение.",
        deal_type=DealType.DAY_RENT,
        is_active=True,
        observed_at=OBSERVED,
    )
    PriceHistory.objects.create(
        ad=rent,
        observed_at=PRICE_RECORDED,
        price_rub=Decimal("4000"),
        price_kind=PriceKind.FROM,
    )
    archived = Ad.objects.create(
        property=item,
        source_id="domclick",
        external_id="listing-removed",
        url="https://example.test/domclick/archive/",
        description="Сохранённое снятое предложение.",
        deal_type=DealType.MONTH_RENT,
        is_active=False,
        photos=[REMOVED_PHOTO],
        photos_observed_at=OBSERVED,
        observed_at=OBSERVED,
    )
    PriceHistory.objects.create(
        ad=archived,
        observed_at=PRICE_RECORDED,
        price_rub=Decimal("80000"),
        price_kind=PriceKind.LISTED,
    )
    removed = Property.objects.create(
        region=item.region, property_type=item.property_type, address="Снятый объект"
    )
    Ad.objects.create(
        property=removed,
        source_id="domclick",
        external_id="entirely-removed",
        url="https://example.test/domclick/entirely-removed/",
        deal_type=DealType.SALE,
        is_active=False,
        observed_at=OBSERVED,
    )
    return item.pk, removed.pk


def route_card_photos(page: Page) -> None:
    def serve_photo(route: Route) -> None:
        if route.request.url == BROKEN_PHOTO:
            route.abort()
        else:
            route.fulfill(content_type="image/png", body=PUBLISHED_PIXEL)

    page.route("https://media.example.test/card/**", serve_photo)


def check_property_card(page: Page, live_server_url: str, ids: tuple[int, int]) -> None:
    # Arrange
    property_id, removed_id = ids
    route_card_photos(page)
    search = (
        f"{live_server_url}/catalog/?deal_type=sale&category=flat&area_min=69&page=2"
    )
    direct = f"{live_server_url}/properties/{property_id}/"

    # Act: open the only result on the second catalog page.
    page.goto(search)
    page.locator("article.result a.object-link").click()

    # Assert: the matching offer is first and histories keep their separate prices.
    expect(page.get_by_role("heading", name="2-комнатная квартира")).to_be_visible()
    expect(page.get_by_role("link", name="Назад к результатам")).to_have_count(0)
    expect(page.get_by_role("link", name="JSON")).to_have_count(0)
    active = page.get_by_role("region", name="Активные объявления")
    expect(active.locator(".publication-header strong")).to_have_text(
        ["Циан", "Авито", "Циан"]
    )
    sale = active.get_by_role("article").filter(has_text="Вторичка")
    expect(active.get_by_role("article").first).to_contain_text("Продажа")
    content = page.get_by_role("region", name="Описания объявлений")
    sale_content = content.get_by_role("article").filter(has_text="Вторичка")
    history = page.get_by_role("region", name="История цен")
    expect(history.locator(".price-chart-estimate")).to_have_count(1)
    points = history.locator("[data-price-marker]")
    expect(points).to_have_count(2)
    expect(history.get_by_text("Все записи")).to_have_count(0)
    points.first.press("Enter")
    expect(history.locator("[data-price-value]")).to_contain_text("17.11.2026")
    expect(history.locator("[data-price-value]")).to_contain_text("от 10 000 000 руб.")
    points.last.press("Enter")
    expect(history.locator("[data-price-value]")).to_contain_text("18.11.2026")
    expect(history.locator("[data-price-value]")).to_contain_text("10 000 000 руб.")
    expect(sale.get_by_text("Данные объявления получены")).to_have_count(0)
    assert sale_content.locator(".seller-description").text_content() == (
        "Первый абзац владельца.\n\nВторой абзац владельца."
    )
    monthly = active.get_by_role("article").filter(has_text="Месячная аренда")
    expect(monthly.get_by_text("Месячная аренда")).to_be_visible()
    expect(monthly.get_by_text("Цена не получена")).to_be_visible()
    daily = active.get_by_role("article").filter(has_text="Суточная аренда")
    expect(daily.locator(".price")).to_have_text("от 4 000 руб./сут.")
    history.get_by_role("combobox").select_option(label="Суточная аренда")
    daily_history = history.locator("[data-price-chart]:visible")
    expect(daily_history.locator("time")).to_have_text("17.11.2026 10:00")
    expect(daily_history).to_contain_text("от 4 000 руб./сут.")

    # Act: a failed first photo, a working second photo, and a removed publication.
    gallery = page.get_by_role("region", name="Фото объявления", exact=True)
    expect(gallery.get_by_role("link", name="Циан")).to_have_count(0)
    current_slide = gallery.locator("[data-slide]:not([hidden])")
    current_slide.scroll_into_view_if_needed()
    expect(current_slide.get_by_text("Фото недоступно")).to_be_visible()
    gallery.get_by_role("button", name="Следующее").click()
    page.get_by_text("Снятые объявления (1)", exact=True).click()

    # Assert: gallery navigation retains source URLs; removed data stays separate.
    expect(current_slide.get_by_role("img", name="Фото 2, Циан")).to_have_js_property(
        "naturalWidth", 1
    )
    expect(current_slide.get_by_role("link", name="Открыть фото 2")).to_have_attribute(
        "href", PUBLISHED_PHOTO
    )
    gallery.get_by_role("button", name="Предыдущее").click()
    expect(current_slide.get_by_text("Фото недоступно")).to_be_visible()
    removed = page.get_by_role("article").filter(has_text="Домклик")
    expect(removed.get_by_text("Снято с публикации")).to_be_visible()
    expect(removed.get_by_role("table")).to_contain_text("80 000 руб./мес.")
    expect(removed.get_by_role("link", name="Открыть фото 1")).to_have_attribute(
        "href", REMOVED_PHOTO
    )

    # Act: go back, open without search context, and open a fully removed object.
    page.go_back()
    expect(page).to_have_url(search)
    page.goto(direct)
    expect(page.get_by_role("link", name="Поиск", exact=True)).to_be_visible()
    removed_page = page.goto(f"{live_server_url}/properties/{removed_id}/")

    # Assert: fully removed property is still readable directly.
    assert removed_page is not None and removed_page.status == 200
    expect(page.get_by_text("Все сохранённые объявления сняты")).to_be_visible()


class PropertyBrowserTests(StaticLiveServerTestCase):
    def test_guest_from_page_two_and_direct_property_card(self) -> None:
        # Arrange
        ids = seed_property_card()

        # Act / Assert
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            check_property_card(context.new_page(), self.live_server_url, ids)

    def test_invalid_context_unknown_id_and_server_error_are_distinct(self) -> None:
        # Arrange
        property_id, _ = seed_property_card()
        path = f"/properties/{property_id}/"
        self.client.raise_request_exception = False

        # Act
        invalid = [
            self.client.get(f"{path}?page=2"),
            self.client.get(f"{path}?from=https://example.test/&page=-1"),
            self.client.get(f"{path}?from=catalog&from=catalog"),
        ]
        mixed_errors = self.client.get(f"{path}?from=catalog&items=1&rooms_min=bad")
        missing = self.client.get(f"/properties/{BIGINT_MAX}/")
        too_large = self.client.get(f"/properties/{BIGINT_MAX + 1}/")
        previous_page = self.client.get(f"{path}?from=catalog&page=5000")
        with patch(
            "app.catalog.views.property_details",
            side_effect=DatabaseError("private connection details"),
        ):
            failed = self.client.get(path)

        # Assert
        for response in invalid:
            self.assertContains(
                response, "Некорректный контекст поиска", status_code=400
            )
            self.assertContains(response, 'href="/catalog/"', status_code=400)
        self.assertContains(
            mixed_errors, "items — Неизвестный параметр.", status_code=400
        )
        self.assertContains(
            mixed_errors, "rooms_min — Введите целое число.", status_code=400
        )
        self.assertContains(missing, "Объект не найден", status_code=404)
        self.assertContains(too_large, "Объект не найден", status_code=404)
        self.assertNotContains(previous_page, "Назад к результатам")
        self.assertContains(failed, "Не удалось загрузить данные", status_code=500)
        self.assertNotContains(failed, "private connection details", status_code=500)


class PropertySnapshotTests(TransactionTestCase):
    def test_price_and_publication_are_read_from_one_snapshot(self) -> None:
        # Arrange: commit an update on another connection between offers and history.
        property_id, _ = seed_property_card()
        sale = Ad.objects.get(property_id=property_id, external_id="listing-sale")
        original_filter = PriceHistory.objects.filter

        def publish_later_observation() -> None:
            try:
                with transaction.atomic():
                    Ad.objects.filter(pk=sale.pk).update(description="Новый снимок.")
                    PriceHistory.objects.create(
                        ad_id=sale.pk,
                        observed_at=OBSERVED,
                        price_rub=Decimal("11000000"),
                        price_kind=PriceKind.LISTED,
                    )
            finally:
                connection.close()

        def history_after_commit(*, ad__in: list[Ad]) -> models.QuerySet[PriceHistory]:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish_later_observation).result(timeout=5)
            return original_filter(ad__in=ad__in)

        # Act
        with patch.object(
            PriceHistory.objects, "filter", side_effect=history_after_commit
        ):
            previous = property_details(property_id)
        current = property_details(property_id)

        # Assert: neither old description/new price nor the opposite mix is visible.
        assert previous is not None and current is not None
        old_offer = next(item for item in previous.offers if item.ad.pk == sale.pk)
        new_offer = next(item for item in current.offers if item.ad.pk == sale.pk)
        assert old_offer.latest_price is not None and new_offer.latest_price is not None
        self.assertEqual(
            (old_offer.ad.description, old_offer.latest_price.price_rub),
            (sale.description, Decimal("10000000")),
        )
        self.assertEqual(
            (new_offer.ad.description, new_offer.latest_price.price_rub),
            ("Новый снимок.", Decimal("11000000")),
        )
