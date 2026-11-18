"""Apply and verify PostgreSQL permissions for the dedicated database."""

import argparse
import sys
from collections import defaultdict

import psycopg
from django.core.exceptions import ImproperlyConfigured
from psycopg import Cursor, sql

from config.database import postgres_database

# The database name is added after connecting. Names here match the migration SQL.
DJANGO_TABLES = (
    "auth_group",
    "auth_group_permissions",
    "auth_permission",
    "auth_user",
    "auth_user_groups",
    "auth_user_user_permissions",
    "django_admin_log",
    "django_content_type",
    "django_session",
)
AD_UPDATES = (
    "url",
    "market_segment",
    "description",
    "is_active",
    "observed_at",
    "photos",
    "photos_observed_at",
)
CATALOG_TABLES = {
    "sources": ("SELECT",),
    "regions": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "property_types": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "properties": ("SELECT", "INSERT", "UPDATE"),
    "ads": ("SELECT", "INSERT"),
    "price_history": ("SELECT", "INSERT"),
}
PHOTOS_FUNCTION = "catalog_photos_valid(jsonb)"
TABLES = {
    **CATALOG_TABLES,
    **dict.fromkeys(DJANGO_TABLES, ("SELECT", "INSERT", "UPDATE", "DELETE")),
    "django_migrations": ("SELECT",),
}
SEQUENCES = (
    "ads",
    "price_history",
    "properties",
    "property_types",
    "regions",
    *(table for table in DJANGO_TABLES if table != "django_session"),
)

# acldefault accounts for implicit ACLs, including PUBLIC EXECUTE on routines.
OBJECTS = """
    SELECT 'DATABASE' AS kind, datname AS name, datdba AS owner,
           coalesce(datacl, acldefault('d', datdba)) AS acl
    FROM pg_database WHERE datname = current_database()
    UNION ALL
    SELECT 'SCHEMA', nspname, nspowner, coalesce(nspacl, acldefault('n', nspowner))
    FROM pg_namespace WHERE nspname = 'public'
    UNION ALL
    SELECT CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
           c.relname, c.relowner, coalesce(c.relacl, acldefault(
               (CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END)::"char", c.relowner))
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
    UNION ALL
    SELECT 'ROUTINE', p.proname || '(' || oidvectortypes(p.proargtypes) || ')',
           p.proowner, coalesce(p.proacl, acldefault('f', p.proowner))
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
"""

# Columns have independent ACLs that survive a relation-level REVOKE.
COLUMN_ACLS = """
    SELECT c.relname, a.attname, acl.grantee, acl.privilege_type, acl.is_grantable
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL aclexplode(a.attacl) acl
    WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
      AND acl.grantee <> c.relowner
"""

type Key = tuple[str, str, str | None]
type Policy = dict[Key, tuple[str, ...]]
type Grant = tuple[Key, str, str, bool]


class RoleError(Exception):
    pass


def policy(database: str) -> Policy:
    return {
        ("DATABASE", database, None): ("CONNECT",),
        ("SCHEMA", "public", None): ("USAGE",),
        **{("TABLE", table, None): grants for table, grants in TABLES.items()},
        ("TABLE", "sources", "enabled"): ("UPDATE",),
        **{("TABLE", "ads", column): ("UPDATE",) for column in AD_UPDATES},
        **{("SEQUENCE", f"{table}_id_seq", None): ("USAGE",) for table in SEQUENCES},
        ("ROUTINE", PHOTOS_FUNCTION, None): ("EXECUTE",),
    }


def validate_owners(cursor: Cursor[tuple[object, ...]], role: str) -> str:
    cursor.execute("""
        SELECT current_database(), current_user, session_user,
               (SELECT count(*) FROM pg_roles
                WHERE rolname IN ('app', 'migrator') AND rolcanlogin
                  AND NOT (rolsuper OR rolcreatedb OR rolcreaterole
                           OR rolreplication OR rolbypassrls)
                  AND oid NOT IN (SELECT member FROM pg_auth_members))
    """)
    identity = cursor.fetchone()
    if identity is None or identity[1:] != (role, role, 2):
        raise RoleError(
            "Unexpected database session identity or role memberships/attributes"
        )

    validate_database_owners(cursor)
    return str(identity[0])


def validate_database_owners(cursor: Cursor[tuple[object, ...]]) -> None:
    cursor.execute("""
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast')
          AND nspname !~ '^pg_(temp|toast_temp)_[0-9]+$'
    """)
    if schema := cursor.fetchone():
        raise RoleError(f"Unexpected schema {schema[0]} in application database")

    cursor.execute(
        sql.SQL("""
            SELECT kind, name, pg_get_userbyid(owner) FROM ({}) objects
            UNION ALL
            SELECT 'TYPE', t.typname, pg_get_userbyid(t.typowner)
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public'
        """).format(sql.SQL(OBJECTS))
    )
    for kind, name, owner in cursor.fetchall():
        if owner != "migrator" and (kind, owner) != ("SCHEMA", "pg_database_owner"):
            raise RoleError(f"Unexpected owner of {kind} {name}: {owner}")

    cursor.execute(
        """
        SELECT hooks.kind, hooks.name, relation.relname
        FROM (
            SELECT 'trigger' AS kind, tgname AS name, tgrelid AS relation
            FROM pg_trigger WHERE NOT tgisinternal
            UNION ALL
            SELECT 'rewrite rule', rulename, ev_class FROM pg_rewrite
        ) hooks
        JOIN pg_class relation ON relation.oid = hooks.relation
        JOIN pg_namespace schema ON schema.oid = relation.relnamespace
        WHERE schema.nspname = 'public' AND relation.relname = ANY(%s::name[])
        ORDER BY relation.relname, hooks.kind, hooks.name
        LIMIT 1
        """,
        ([table for table in TABLES if table != "django_migrations"],),
    )
    if hook := cursor.fetchone():
        kind, name, table = hook
        raise RoleError(f"Unexpected {kind} {name} on app-writable table {table}")


def existing_grants(cursor: Cursor[tuple[object, ...]]) -> set[Grant]:
    cursor.execute(
        sql.SQL("""
            SELECT obj.kind, obj.name, NULL::name AS column_name,
                   CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                        ELSE pg_get_userbyid(acl.grantee) END,
                   acl.privilege_type, acl.is_grantable
            FROM ({objects}) obj CROSS JOIN LATERAL aclexplode(obj.acl) acl
            WHERE acl.grantee <> obj.owner
            UNION ALL
            SELECT 'TABLE', col.relname, col.attname,
                   CASE WHEN col.grantee = 0 THEN 'PUBLIC'
                        ELSE pg_get_userbyid(col.grantee) END,
                   col.privilege_type, col.is_grantable
            FROM ({columns}) col
        """).format(objects=sql.SQL(OBJECTS), columns=sql.SQL(COLUMN_ACLS))
    )
    return {
        (
            (str(kind), str(name), str(column) if column is not None else None),
            str(grantee),
            str(privilege),
            grantable is True,
        )
        for kind, name, column, grantee, privilege, grantable in cursor.fetchall()
    }


def grant_target(key: Key) -> sql.Composable:
    kind, name, _ = key
    # Only the exact catalog CHECK helper is granted execution.
    if kind == "ROUTINE":
        return sql.SQL("FUNCTION public.catalog_photos_valid(pg_catalog.jsonb)")
    name_parts = ("public", name) if kind in {"TABLE", "SEQUENCE"} else (name,)
    return sql.SQL("{} {}").format(sql.SQL(kind), sql.Identifier(*name_parts))


def apply_grants(cursor: Cursor[tuple[object, ...]], database: str) -> None:
    by_table: dict[str, set[str]] = defaultdict(set)
    for (_, table, column), grantee, _, _ in existing_grants(cursor):
        if column is not None and grantee in {"app", "PUBLIC"}:
            by_table[table].add(column)

    for table, columns in by_table.items():
        cursor.execute(
            sql.SQL("REVOKE ALL ({}) ON TABLE {} FROM app, PUBLIC CASCADE").format(
                sql.SQL(", ").join(map(sql.Identifier, sorted(columns))),
                sql.Identifier("public", table),
            )
        )

    cursor.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM app, PUBLIC CASCADE").format(
            sql.Identifier(database)
        )
    )
    cursor.execute("REVOKE ALL ON SCHEMA public FROM app, PUBLIC CASCADE")
    cursor.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app, PUBLIC CASCADE")
    cursor.execute(
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app, PUBLIC CASCADE"
    )
    cursor.execute(
        "REVOKE ALL ON ALL ROUTINES IN SCHEMA public FROM app, PUBLIC CASCADE"
    )

    for key, privileges in policy(database).items():
        privilege_list = sql.SQL(", ").join(map(sql.SQL, privileges))
        if (column := key[2]) is not None:
            privilege_list += sql.SQL(" ({})").format(sql.Identifier(column))
        cursor.execute(
            sql.SQL("GRANT {} ON {} TO app").format(privilege_list, grant_target(key))
        )


def verify_grants(cursor: Cursor[tuple[object, ...]], database: str) -> None:
    expected = {
        (key, "app", privilege, False)
        for key, privileges in policy(database).items()
        for privilege in privileges
    }
    actual = existing_grants(cursor)
    if actual != expected:
        missing = sorted(map(str, expected - actual))
        extra = sorted(map(str, actual - expected))
        raise RoleError(f"ACL mismatch. Missing: {missing}. Unexpected: {extra}")

    cursor.execute("""
        SELECT p.prosecdef
        FROM pg_proc p
        WHERE p.oid = to_regprocedure('public.catalog_photos_valid(pg_catalog.jsonb)')
    """)
    if cursor.fetchone() != (False,):
        raise RoleError("The catalog photos CHECK helper must use SECURITY INVOKER")

    cursor.execute("""
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = r.relnamespace
        WHERE c.contype = 'f' AND n.nspname = 'public'
          AND r.relname IN ('ads', 'properties', 'price_history')
          AND (c.confupdtype NOT IN ('a', 'r') OR c.confdeltype NOT IN ('a', 'r'))
    """)
    if cursor.fetchone() is not None:
        raise RoleError("Catalog foreign keys must reject cascading changes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "check"))
    args = parser.parse_args()
    writing = args.action == "apply"
    role = "migrator" if writing else "app"
    password_variable = "DB_MIGRATOR_PASSWORD" if writing else "DB_APP_PASSWORD"

    # Reuse the same parameters as the corresponding Django settings.
    try:
        settings = postgres_database(role, password_variable)
        with psycopg.connect(
            dbname=settings["NAME"],
            user=settings["USER"],
            password=settings["PASSWORD"],
            host=settings["HOST"],
            port=settings["PORT"],
            connect_timeout=5,
        ) as connection:
            connection.read_only = not writing
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL search_path = pg_catalog")
                database = validate_owners(cursor, role)
                if writing:
                    apply_grants(cursor, database)
                verify_grants(cursor, database)
    except (ImproperlyConfigured, RoleError, psycopg.Error) as error:
        print(f"Database roles: {error}", file=sys.stderr)
        return 1

    print(f"Database roles: ACL verified as {role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
