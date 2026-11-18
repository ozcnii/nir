"""Prevent write scenarios from running outside the disposable Compose database."""

import os

from django.db.backends.utils import CursorWrapper
from psycopg import Cursor
from psycopg.rows import TupleRow


def require_fixture_database(cursor: CursorWrapper | Cursor[TupleRow]) -> None:
    token = os.environ["NIR_PG_FIXTURE_TOKEN"]
    cursor.execute(
        """
        SELECT current_database(), current_user, session_user,
               current_setting('server_version_num'),
               shobj_description(oid, 'pg_database')
        FROM pg_database WHERE datname = current_database()
        """
    )
    if cursor.fetchone() != (
        "t15_catalog_test",
        "app",
        "app",
        "170012",
        "nir-f25:" + token,
    ):
        raise RuntimeError("Expected the disposable Compose test database.")
