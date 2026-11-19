from secrets import token_urlsafe

from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from app.catalog.models import Source


class AdminBrowserTests(StaticLiveServerTestCase):
    def test_native_login_source_form_and_logout(self) -> None:
        # Arrange
        login, password = "operator", token_urlsafe()
        User.objects.create_superuser(login, password=password)
        Source.objects.get_or_create(code="cian", defaults={"enabled": True})
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            page = context.new_page()

            # Act
            response = page.goto(f"{self.live_server_url}{reverse('admin:index')}")
            username = page.locator('input[name="username"]')
            username.fill(login)
            page.locator('input[name="password"]').fill("incorrect")
            page.locator('input[type="submit"]').click()
            stylesheet = page.request.get(
                f"{self.live_server_url}/static/admin/css/base.css"
            )

            # Assert
            assert response is not None
            self.assertEqual(response.status, 200)
            self.assertTrue(page.url.endswith("/admin/login/?next=/admin/"))
            expect(username).to_have_value(login)
            expect(page.locator(".errornote")).to_be_visible()
            self.assertEqual(stylesheet.status, 200)
            self.assertTrue(stylesheet.headers["content-type"].startswith("text/css"))

            # Act
            page.locator('input[name="password"]').fill(password)
            page.locator('input[type="submit"]').click()
            source = reverse("admin:catalog_source_change", args=["cian"])
            page.goto(f"{self.live_server_url}{source}")

            # Assert
            expect(page.get_by_text("Циан", exact=True)).to_be_visible()
            expect(page.locator('input[name="enabled"]')).to_be_checked()
            expect(page.locator('input[name="code"]')).to_have_count(0)

            # Act
            logout = reverse("admin:logout")
            page.locator(f'form[action="{logout}"] button').click()
            page.goto(f"{self.live_server_url}{source}")

            # Assert
            expect(page.locator('input[name="username"]')).to_be_visible()
