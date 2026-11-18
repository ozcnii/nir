"""Own the PostgreSQL singleton session and report controlled lifecycle events."""

import os
import signal
import sys
import time
from collections.abc import Callable

import django
import psycopg
from django.db import DatabaseError

from collection.selection import Direction, InvalidSelection, Selection, parse, validate
from config.collection_lock import LockBusy, LockLost, RunSession, run_session
from config.control import ControlError, decode, encode

RunBody = Callable[[RunSession, Selection, Callable[[], bool]], None]


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

    ready = False
    try:
        with run_session() as session:
            validate(selection, supported)
            session.require_lock()
            if not stopped():
                ready = True
                emit("ready")
                body(session, selection, stopped)
                session.require_lock()
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


def idle_run(
    session: RunSession, _selection: Selection, stopped: Callable[[], bool]
) -> None:
    while not stopped():
        session.require_lock()
        time.sleep(0.2)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise InvalidSelection("invalid_selection")
        selection = parse(decode(sys.argv[1].encode()))
    except (ControlError, InvalidSelection):
        emit("finished", "invalid_selection")
        return 0

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    return run(selection, supported=unavailable_direction, body=idle_run)


if __name__ == "__main__":
    raise SystemExit(main())
