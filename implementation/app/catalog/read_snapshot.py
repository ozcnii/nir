"""Open a consistent, read-only PostgreSQL snapshot for catalog queries."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


@contextmanager
def read_snapshot() -> Iterator[None]:
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield
