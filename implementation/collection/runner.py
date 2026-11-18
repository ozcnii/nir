"""Own the PostgreSQL singleton session and report controlled lifecycle events."""

import os
import signal
import sys
from collections.abc import Callable

import django
import psycopg
from django.db import DatabaseError, connection
from playwright.sync_api import Error as PlaywrightError

from collection.browser import (
    BrowserCleanupUnconfirmed,
    BrowserFailed,
    BrowserWork,
    Stopped,
    browser_work,
)
from collection.selection import Direction, InvalidSelection, Selection, parse, validate
from config.collection_lock import LockBusy, LockLost, RunSession, run_session
from config.control import ADMISSION_FD, ControlError, decode, encode

RunBody = Callable[
    [RunSession, Selection, Callable[[], bool], Callable[[], None]], None
]
BrowserBody = Callable[[BrowserWork, Selection], None]


def emit(event: str, reason: str = "") -> None:
    sys.stdout.buffer.write(encode({"event": event, "reason": reason}))
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

    django.setup()
    ready = False
    try:
        with run_session() as session:
            try:
                validate(selection, supported)
            finally:
                # Reference lookups use Django's separate read connection.
                connection.close()

            def initialized() -> None:
                nonlocal ready
                if stopped():
                    raise Stopped
                session.require_lock()
                if stopped():
                    raise Stopped
                ready = True
                emit("ready")

            if not stopped():
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
        with browser_work(session, stopped) as work:
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
