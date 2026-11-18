"""Own the PostgreSQL singleton session and report controlled lifecycle events."""

import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Literal

import django
import psycopg
from django.db import DatabaseError, connection
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from psycopg import Connection
from psycopg.rows import TupleRow

from collection.browser import (
    NAVIGATION_TIMEOUT_MS,
    BrowserCleanupUnconfirmed,
    BrowserFailed,
    BrowserPages,
    browser_pages,
)
from collection.selection import Direction, InvalidSelection, Selection, parse, validate
from config.collection_lock import LockBusy, LockLost, RunSession, run_session
from config.control import ADMISSION_FD, ControlError, decode, encode
from config.run_events import INSTANCE_ID_ENV, RUN_ID_ENV, SCHEMA_VERSION

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


def check_stop(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise Stopped


class BrowserWork:
    def __init__(
        self, pages: BrowserPages, session: RunSession, stopped: Callable[[], bool]
    ) -> None:
        self.pages = pages
        self._session = session
        self._stopped = stopped
        self._committed_writes = 0

    def checkpoint(self) -> None:
        check_stop(self._stopped)
        self._session.require_lock()
        if not self.pages.connected():
            raise BrowserFailed("browser_disconnected")
        check_stop(self._stopped)

    def navigate(
        self, page: Literal["listing", "card"], url: str
    ) -> tuple[Response | None, datetime, float]:
        for retry in (False, True):
            self.checkpoint()
            observed_at = datetime.now(UTC)
            deadline = time.monotonic() + NAVIGATION_TIMEOUT_MS / 1000
            try:
                response = self.pages.goto(page, url)
            except PlaywrightError as error:
                transport_error = isinstance(error, PlaywrightTimeout) or any(
                    candidate in str(error) for candidate in TRANSPORT_ERRORS
                )
                if retry or not transport_error:
                    raise
            else:
                if retry or response is None or not 500 <= response.status < 600:
                    self.checkpoint()
                    return response, observed_at, deadline
        raise AssertionError("The second navigation always returns or raises")

    def write[T](self, operation: Callable[[Connection[TupleRow]], T]) -> T:
        self.checkpoint()
        # This owns the transaction boundary. Catalog services supply the write.
        with self._session.transaction() as connection:
            check_stop(self._stopped)
            result = operation(connection)
        self._committed_writes += 1
        emit("progress", committed_writes=self._committed_writes)
        return result


RunBody = Callable[
    [RunSession, Selection, Callable[[], bool], Callable[[], None]], None
]
BrowserBody = Callable[[BrowserWork, Selection], None]


_sequence = count(1)


def emit(event: str, reason: str = "", **fields: object) -> None:
    sys.stdout.buffer.write(
        encode(
            {
                **fields,
                "event": event,
                "reason": reason,
                "schema_version": SCHEMA_VERSION,
                "run_id": os.environ[RUN_ID_ENV],
                "instance_id": os.environ[INSTANCE_ID_ENV],
                "seq": next(_sequence),
            }
        )
    )
    sys.stdout.buffer.flush()


def run(
    selection: Selection,
    *,
    supported: Callable[[Direction], bool],
    body: RunBody,
) -> int:
    stop_requested = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    def stopped() -> bool:
        return stop_requested

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    admission = os.environ.pop(ADMISSION_FD, None)
    if admission is None:
        emit("fault", "runner_admission_missing")
        return 1
    with os.fdopen(int(admission), "rb") as gate:
        emit("armed")
        permitted = gate.read(1) == b"1"
    if not permitted or stopped():
        emit("finished", "stopped")
        return 0

    ready = False
    try:
        django.setup()
        check_stop(stopped)
        with run_session() as session:
            check_stop(stopped)
            try:
                validate(selection, supported)
            finally:
                # Reference lookups use Django's separate read connection.
                connection.close()

            def initialized() -> None:
                nonlocal ready
                check_stop(stopped)
                session.require_lock()
                check_stop(stopped)
                ready = True
                emit("ready")

            check_stop(stopped)
            body(session, selection, stopped, initialized)
            session.require_lock()
    except Stopped:
        emit("finished", "stopped")
        return 0
    except BrowserCleanupUnconfirmed as error:
        emit("fault", str(error))
        return 1
    except BrowserFailed as error:
        emit("finished", str(error))
        return 0
    except PlaywrightError:
        emit("finished", "browser_operation_failed")
        return 0
    except InvalidSelection as error:
        if ready:
            emit("fault", "run_failed")
            return 1
        emit("finished", str(error))
        return 0
    except LockBusy:
        emit("finished", "lock_busy")
        return 0
    except LockLost:
        emit("fault", "lock_lost")
        return 1
    except (psycopg.Error, DatabaseError):
        emit("fault", "database_error")
        return 1
    except Exception:
        emit("fault", "run_failed")
        return 1

    emit("finished", "stopped" if stopped() else "completed")
    return 0


def unavailable_direction(_direction: Direction) -> bool:
    # Real source adapters supply supported directions when implemented.
    return False


def run_with_browser(
    selection: Selection, *, supported: Callable[[Direction], bool], body: BrowserBody
) -> int:
    def start_browser(
        session: RunSession,
        snapshot: Selection,
        stopped: Callable[[], bool],
        initialized: Callable[[], None],
    ) -> None:
        with browser_pages(lambda: check_stop(stopped)) as pages:
            work = BrowserWork(pages, session, stopped)
            initialized()
            work.checkpoint()
            body(work, snapshot)

    return run(selection, supported=supported, body=start_browser)


def unavailable_body(_work: BrowserWork, _selection: Selection) -> None:
    raise BrowserFailed("unsupported_direction")


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise InvalidSelection("invalid_selection")
        selection = parse(decode(sys.argv[1].encode()))
    except (ControlError, InvalidSelection):
        emit("finished", "invalid_selection")
        return 0

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    return run_with_browser(
        selection, supported=unavailable_direction, body=unavailable_body
    )


if __name__ == "__main__":
    raise SystemExit(main())
