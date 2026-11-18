"""Coordinate collection, catalog writes and reference deletion in PostgreSQL."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction

_COLLECTION_KEY = (int.from_bytes(b"NIR"), 1)


class CollectionBusy(RuntimeError):
    pass


@contextmanager
def run_session() -> Iterator[None]:
    """Hold the collection lock on the foreground command's Django connection."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", _COLLECTION_KEY)
            if cursor.fetchone() != (True,):
                raise CollectionBusy("collection or catalog operation already running")
        yield
    finally:
        connection.close()


@contextmanager
def catalog_transaction(*, wait: bool) -> Iterator[None]:
    """Hold the shared key from before the operation until its transaction ends."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            if wait:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", _COLLECTION_KEY)
            else:
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(%s, %s)", _COLLECTION_KEY
                )
                if cursor.fetchone() != (True,):
                    raise CollectionBusy(
                        "collection or catalog operation already running"
                    )
        yield
