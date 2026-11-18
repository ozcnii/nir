"""PostgreSQL advisory keys and ownership of the collection session."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from django.db import connection
from psycopg.rows import TupleRow, tuple_row

# Keep the namespace and IDs stable while other processes may hold these locks.
_LOCK_NAMESPACE = int.from_bytes(b"NIR")
_COLLECTION_LOCK = (_LOCK_NAMESPACE, 1)
CATALOG_WRITE_LOCK = (_LOCK_NAMESPACE, 2)


class LockBusy(RuntimeError):
    pass


class LockLost(RuntimeError):
    pass


class RunSession:
    def __init__(self, session: psycopg.Connection[TupleRow]) -> None:
        self._session = session

    def require_lock(self) -> None:
        # Never reconnect or silently reacquire: a different session may own the run.
        try:
            row = self._session.execute(
                """
                SELECT EXISTS (
                    SELECT FROM pg_locks
                    WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                      AND classid = %s::oid AND objid = %s::oid AND objsubid = 2
                      AND mode = 'ExclusiveLock' AND granted
                )
                """,
                _COLLECTION_LOCK,
            ).fetchone()
        except psycopg.Error as error:
            raise LockLost("collection session is unavailable") from error
        if row != (True,):
            raise LockLost("collection session no longer holds its advisory key")

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[TupleRow]]:
        """Fence writes on the physical connection that owns this run's lock."""
        try:
            with self._session.transaction():
                self.require_lock()
                yield self._session
                self.require_lock()
        except psycopg.OperationalError as error:
            raise LockLost(
                "collection session was lost during a transaction"
            ) from error


@contextmanager
def run_session() -> Iterator[RunSession]:
    settings = connection.settings_dict
    with psycopg.connect(
        dbname=settings["NAME"],
        user=settings["USER"],
        password=settings["PASSWORD"],
        host=settings["HOST"],
        port=settings["PORT"],
        connect_timeout=3,
        options="-c statement_timeout=1500",
        application_name="nir-collection",
        autocommit=True,
        row_factory=tuple_row,
    ) as session:
        result = session.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", _COLLECTION_LOCK
        ).fetchone()
        if result != (True,):
            raise LockBusy("collection advisory key is already held")
        yield RunSession(session)


def try_delete_lock() -> bool:
    # The caller owns the surrounding transaction and retains the key through commit.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", _COLLECTION_LOCK)
        row: object = cursor.fetchone()
        return row == (True,)
