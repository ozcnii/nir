"""Read a card projection within the caller's Playwright attempt."""

import time

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout


def wait_for_card_data(
    page: Page, expression: str, deadline: float, *, arg: str | None = None
) -> object:
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    handle = page.wait_for_function(expression, arg=arg, timeout=remaining_ms)
    try:
        data = handle.json_value()
    finally:
        handle.dispose()
    if time.monotonic() >= deadline:
        raise PlaywrightTimeout("Card attempt deadline elapsed")
    return data
