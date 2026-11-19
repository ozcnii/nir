"""Open a headless Chromium page for the collector."""

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Page, sync_playwright


@contextmanager
def browser_page() -> Iterator[Page]:
    with (
        sync_playwright() as playwright,
        playwright.chromium.launch(
            channel="chromium",
            headless=True,
            chromium_sandbox=True,
            handle_sigint=False,
            handle_sigterm=False,
        ) as browser,
        browser.new_context(accept_downloads=False) as context,
    ):
        yield context.new_page()
