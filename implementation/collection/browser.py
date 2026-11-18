"""Own a run's Chromium resources."""

import ctypes
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal

from playwright.sync_api import (
    Browser,
    Error,
    Page,
    Response,
    sync_playwright,
)

NAVIGATION_TIMEOUT_MS = 30_000
CHILD_CLEANUP_SECONDS = 3
PR_SET_CHILD_SUBREAPER = 36


class BrowserFailed(RuntimeError):
    pass


class BrowserCleanupUnconfirmed(BrowserFailed):
    pass


class BrowserPages:
    def __init__(self, browser: Browser, listing: Page, card: Page) -> None:
        self._browser = browser
        self.listing = listing
        self.card = card

    def connected(self) -> bool:
        return self._browser.is_connected()

    def goto(self, page: Literal["listing", "card"], url: str) -> Response | None:
        target = self.listing if page == "listing" else self.card
        return target.goto(
            url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
        )


def adopt_orphans() -> None:
    # Linux re-parents orphaned descendants to this runner. The wait below can
    # then verify just our subtree, including a Chromium child in its own group.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def wait_for_children() -> None:
    deadline = time.monotonic() + CHILD_CLEANUP_SECONDS
    while True:
        try:
            result = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            raise BrowserCleanupUnconfirmed("browser_children_unconfirmed")
        if result is None:
            time.sleep(0.02)


@contextmanager
def browser_pages(checkpoint: Callable[[], None]) -> Iterator[BrowserPages]:
    checkpoint()
    try:
        adopt_orphans()
        profile = Path(tempfile.mkdtemp(prefix="nir-browser-"))
    except OSError as error:
        raise BrowserFailed("browser_initialization_failed") from error
    previous_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(profile)
    failed_cleanup = False

    def close(action: Callable[[], object]) -> None:
        nonlocal failed_cleanup
        try:
            action()
        except (Error, OSError, AttributeError):
            failed_cleanup = True

    try:
        with ExitStack() as resources:
            manager = sync_playwright()
            resources.callback(close, lambda: manager.__exit__(None, None, None))
            try:
                playwright = manager.start()
                checkpoint()
                browser = playwright.chromium.launch(
                    channel="chromium",
                    headless=True,
                    chromium_sandbox=True,
                    handle_sigint=False,
                    handle_sigterm=False,
                    handle_sighup=False,
                )
                resources.callback(close, browser.close)
                checkpoint()
                context = browser.new_context(accept_downloads=False)
                resources.callback(close, context.close)
                checkpoint()
                listing = context.new_page()
                checkpoint()
                card = context.new_page()
            except Error as error:
                raise BrowserFailed("browser_initialization_failed") from error

            try:
                yield BrowserPages(browser, listing, card)
            finally:
                if not browser.is_connected():
                    failed_cleanup = True
    finally:
        if previous_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmp
        # Leave the known directory intact when a live child might still use it.
        try:
            wait_for_children()
            shutil.rmtree(profile)
        except OSError as error:
            raise BrowserCleanupUnconfirmed("browser_cleanup_unconfirmed") from error
        if failed_cleanup:
            raise BrowserCleanupUnconfirmed("browser_cleanup_unconfirmed")
