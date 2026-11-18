"""Own a run's Chromium resources and check the boundaries between work steps."""

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
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeout,
)
from psycopg import Connection
from psycopg.rows import TupleRow

from config.collection_lock import RunSession

NAVIGATION_TIMEOUT_MS = 30_000
CHILD_CLEANUP_SECONDS = 3
PR_SET_CHILD_SUBREAPER = 36
TRANSPORT_ERRORS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_ABORTED",
    "net::ERR_TIMED_OUT",
    "net::ERR_NETWORK_CHANGED",
)


class Stopped(RuntimeError):
    pass


class BrowserFailed(RuntimeError):
    pass


class BrowserCleanupUnconfirmed(BrowserFailed):
    pass


class BrowserWork:
    def __init__(
        self,
        browser: Browser,
        listing: Page,
        card: Page,
        session: RunSession,
        stopped: Callable[[], bool],
    ) -> None:
        self._browser = browser
        self.listing = listing
        self.card = card
        self._session = session
        self._stopped = stopped

    def checkpoint(self) -> None:
        if self._stopped():
            raise Stopped
        self._session.require_lock()
        if not self._browser.is_connected():
            raise BrowserFailed("browser_disconnected")
        if self._stopped():
            raise Stopped

    def navigate(self, page: Literal["listing", "card"], url: str) -> Response | None:
        target = self.listing if page == "listing" else self.card
        for retry in (False, True):
            self.checkpoint()
            try:
                response = target.goto(
                    url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
                )
            except Error as error:
                transport_error = isinstance(error, PlaywrightTimeout) or any(
                    candidate in str(error) for candidate in TRANSPORT_ERRORS
                )
                if retry or not transport_error:
                    raise
            else:
                if retry or response is None or not 500 <= response.status < 600:
                    self.checkpoint()
                    return response
        raise AssertionError("The second navigation always returns or raises")

    def write[T](self, operation: Callable[[Connection[TupleRow]], T]) -> T:
        self.checkpoint()
        with self._session.transaction() as connection:
            if self._stopped():
                raise Stopped
            return operation(connection)


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
def browser_work(
    session: RunSession, stopped: Callable[[], bool]
) -> Iterator[BrowserWork]:
    if stopped():
        raise Stopped
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
                if stopped():
                    raise Stopped
                browser = playwright.chromium.launch(
                    channel="chromium",
                    headless=True,
                    chromium_sandbox=True,
                    handle_sigint=False,
                    handle_sigterm=False,
                    handle_sighup=False,
                )
                resources.callback(close, browser.close)
                if stopped():
                    raise Stopped
                context = browser.new_context(accept_downloads=False)
                resources.callback(close, context.close)
                if stopped():
                    raise Stopped
                listing = context.new_page()
                if stopped():
                    raise Stopped
                card = context.new_page()
            except Error as error:
                raise BrowserFailed("browser_initialization_failed") from error

            try:
                yield BrowserWork(browser, listing, card, session, stopped)
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
