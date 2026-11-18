from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright


class AdminBrowserTests(StaticLiveServerTestCase):
    def test_login_screen_serves_django_static_and_accepts_input(self) -> None:
        # Arrange: Django serves the admin and its static assets on a local port.
        with (
            sync_playwright() as playwright,
            playwright.chromium.launch(channel="chromium") as browser,
            browser.new_context() as context,
        ):
            page = context.new_page()

            # Act
            response = page.goto(f"{self.live_server_url}/admin/")
            username = page.locator('input[name="username"]')
            username.fill("ci-fixture")
            stylesheet = page.request.get(
                f"{self.live_server_url}/static/admin/css/base.css"
            )

            # Assert
            assert response is not None
            self.assertEqual(response.status, 200)
            self.assertTrue(page.url.endswith("/admin/login/?next=/admin/"))
            expect(username).to_have_value("ci-fixture")
            expect(page.locator('input[type="submit"]')).to_be_visible()
            self.assertEqual(stylesheet.status, 200)
            self.assertTrue(stylesheet.headers["content-type"].startswith("text/css"))
