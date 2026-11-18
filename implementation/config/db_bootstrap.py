"""Prepare the dedicated database and roles in an operator-only process."""

import os
import sys

import psycopg
from django.core.exceptions import ImproperlyConfigured
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo

from config.database import postgres_database
from config.db_roles import RoleError, validate_database_owners


def authenticate(role: str, password: str, database: str) -> None:
    with psycopg.connect(
        dbname=database,
        user=role,
        password=password,
        host="db",
        connect_timeout=5,
        require_auth="scram-sha-256",
    ) as connection:
        if connection.execute("SELECT current_user, session_user").fetchone() != (
            role,
            role,
        ):
            raise RoleError(f"Unexpected identity authenticating {role}")


def create_roles(admin: Connection[tuple[object, ...]], roles: dict[str, str]) -> None:
    if not roles:
        return
    # libpq derives SCRAM verifiers locally. Only these statements suppress server
    # logging, including duration, error statements and sampled transactions.
    try:
        with admin.transaction():
            admin.execute("""
                SET LOCAL log_min_messages = 'panic';
                SET LOCAL log_min_error_statement = 'panic';
                SET LOCAL log_statement = 'none';
                SET LOCAL log_duration = off;
                SET LOCAL log_min_duration_statement = -1;
                SET LOCAL log_min_duration_sample = -1;
                SET LOCAL log_transaction_sample_rate = 0;
                SET LOCAL track_activities = off;
            """)
            for role, password in roles.items():
                verifier = admin.pgconn.encrypt_password(
                    password.encode(), role.encode(), b"scram-sha-256"
                ).decode()
                admin.execute(
                    sql.SQL("""
                        CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOREPLICATION NOBYPASSRLS PASSWORD {}
                    """).format(sql.Identifier(role), sql.Literal(verifier))
                )
    except psycopg.Error as error:
        # A driver exception may include the SQL containing the SCRAM verifier.
        raise RoleError(
            f"Could not create login roles (SQLSTATE {error.sqlstate})"
        ) from None


def bootstrap() -> None:
    passwords = {
        "migrator": postgres_database("migrator", "DB_MIGRATOR_PASSWORD")["PASSWORD"],
        "app": postgres_database("app", "DB_APP_PASSWORD")["PASSWORD"],
    }
    target = os.environ["DB_NAME"]
    connection_options = make_conninfo(
        host="db",
        user="postgres",
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
        require_auth="scram-sha-256",
    )
    with psycopg.connect(
        connection_options,
        dbname="postgres",
        autocommit=True,
    ) as admin:
        # The session lock lives in the maintenance database across CREATE DATABASE.
        admin.execute("SELECT pg_catalog.pg_advisory_lock(1981687401, 1)")
        admin.execute("SET search_path = pg_catalog")
        limit = admin.execute("SHOW max_identifier_length").fetchone()
        if (
            target in {"", "postgres", "template0", "template1"}
            or limit is None
            or len(target.encode()) > int(limit[0])
        ):
            raise RoleError("DB_NAME must name a dedicated application database")

        target_database = admin.execute(
            """
            SELECT pg_get_userbyid(datdba), datistemplate, datallowconn
            FROM pg_database WHERE datname = %s
        """,
            (target,),
        ).fetchone()
        if target_database is not None:
            if target_database != ("migrator", False, True):
                raise RoleError(
                    f"Database {target} conflicts with the expected migrator ownership or attributes"
                )
            with psycopg.connect(
                connection_options,
                dbname=target,
            ) as database:
                database.read_only = True
                with database.cursor() as cursor:
                    cursor.execute("SET LOCAL search_path = pg_catalog")
                    validate_database_owners(cursor)

        missing = passwords.copy()
        for role, normal, can_connect in admin.execute(
            """
            SELECT rolname,
                   rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole
                       OR rolreplication OR rolbypassrls)
                       AND oid NOT IN (SELECT member FROM pg_auth_members),
                   CASE WHEN EXISTS (SELECT FROM pg_database WHERE datname = %s)
                        THEN has_database_privilege(oid, %s, 'CONNECT') ELSE false END
            FROM pg_roles WHERE rolname IN ('migrator', 'app')
        """,
            (target, target),
        ).fetchall():
            if not normal:
                raise RoleError(
                    f"Existing {role} has conflicting attributes or memberships"
                )
            # If the target is missing or its CONNECT grant needs repair, prove
            # the password against this same cluster's maintenance database.
            authenticate(role, missing.pop(role), target if can_connect else "postgres")

        create_roles(admin, missing)
        if target_database is None:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER migrator TEMPLATE template0").format(
                    sql.Identifier(target)
                )
            )


def main() -> int:
    try:
        bootstrap()
    except (KeyError, ImproperlyConfigured, RoleError, psycopg.Error) as error:
        print(f"Database bootstrap: {error}", file=sys.stderr)
        return 1
    print("Database bootstrap: dedicated database and login identities checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
