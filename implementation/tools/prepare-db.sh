#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -z "${POSTGRES_PASSWORD:-}" || -z "${DB_APP_PASSWORD:-}" || -z "${DB_MIGRATOR_PASSWORD:-}" ]]; then
    echo 'Set POSTGRES_PASSWORD, DB_APP_PASSWORD and DB_MIGRATOR_PASSWORD.' >&2
    exit 1
fi
export DB_NAME="${DB_NAME:-catalog}"

docker compose exec -T -e DB_NAME -e DB_APP_PASSWORD -e DB_MIGRATOR_PASSWORD db sh -ec '
    export PGREQUIREAUTH=scram-sha-256 PGPASSWORD="$POSTGRES_PASSWORD"
    exec psql -Xq -h db -U postgres -d postgres -v ON_ERROR_STOP=1
' <<'SQL'
\getenv database DB_NAME
\getenv app_password DB_APP_PASSWORD
\getenv migrator_password DB_MIGRATOR_PASSWORD
\set VERBOSITY terse
SELECT set_config('nir.bootstrap_db', :'database', false) AS target
\gset
DO $$
BEGIN
    IF current_setting('nir.bootstrap_db') IN ('postgres', 'template0', 'template1')
       OR octet_length(current_setting('nir.bootstrap_db'))
          > current_setting('max_identifier_length')::int THEN
        RAISE EXCEPTION 'DB_NAME must name a dedicated application database';
    END IF;
    IF EXISTS (
        SELECT FROM pg_database
        WHERE datname = current_setting('nir.bootstrap_db')
          AND (pg_get_userbyid(datdba) <> 'migrator' OR datistemplate OR NOT datallowconn)
    ) THEN
        RAISE EXCEPTION 'Application database has an unexpected owner or attributes';
    END IF;
    IF EXISTS (
        SELECT FROM pg_roles
        WHERE rolname IN ('app', 'migrator')
          AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
               OR rolreplication OR rolbypassrls
               OR oid IN (SELECT member FROM pg_auth_members))
    ) THEN
        RAISE EXCEPTION 'Application roles have unexpected attributes or memberships';
    END IF;
END
$$;

-- Passwords only appear in the provisioning session. Suppress statement
-- logging and activity reporting for the two generated CREATE ROLE commands.
SET log_min_messages = 'panic';
SET log_min_error_statement = 'panic';
SET log_statement = 'none';
SET log_duration = off;
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET track_activities = off;
SET password_encryption = 'scram-sha-256';
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', role, password)
FROM (VALUES ('migrator', :'migrator_password'), ('app', :'app_password')) AS roles(role, password)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role)
\gexec

SELECT format('CREATE DATABASE %I OWNER migrator TEMPLATE template0', :'database')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'database')
\gexec
SQL

# The migrator owns the schema. Its password stays outside the running app.
docker compose run --rm --no-deps -T -e DB_MIGRATOR_PASSWORD \
    -e DB_APP_PASSWORD= -e DJANGO_SECRET_KEY= app \
    python -m app.manage migrate --settings=app.config.settings_migrator --noinput

docker compose exec -T -e DB_NAME -e DB_MIGRATOR_PASSWORD db sh -ec '
    export PGREQUIREAUTH=scram-sha-256 PGDATABASE="$DB_NAME" PGPASSWORD="$DB_MIGRATOR_PASSWORD"
    exec psql -Xq -h db -U migrator -v ON_ERROR_STOP=1
' <<'SQL'
\getenv database DB_NAME
BEGIN;
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM pg_namespace
        WHERE nspname = 'public'
          AND pg_get_userbyid(nspowner) NOT IN (current_user, 'pg_database_owner')
    ) OR EXISTS (
        SELECT FROM pg_class AS relation
        JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
        WHERE schema.nspname = 'public' AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
          AND relation.relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
    ) OR EXISTS (
        SELECT FROM pg_proc AS routine
        JOIN pg_namespace AS schema ON schema.oid = routine.pronamespace
        WHERE schema.nspname = 'public'
          AND (routine.proowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)
               OR (routine.oid = 'public.catalog_photos_valid(jsonb)'::regprocedure
                   AND routine.prosecdef))
    ) THEN
        RAISE EXCEPTION 'Application schema has an unexpected owner or SECURITY DEFINER helper';
    END IF;
END
$$;
SELECT format('REVOKE ALL (%s) ON TABLE %I.%I FROM app, PUBLIC CASCADE',
              string_agg(quote_ident(attribute.attname), ', '), schema.nspname, relation.relname)
FROM pg_attribute AS attribute
JOIN pg_class AS relation ON relation.oid = attribute.attrelid
JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
WHERE schema.nspname = 'public' AND attribute.attnum > 0
  AND NOT attribute.attisdropped AND attribute.attacl IS NOT NULL
GROUP BY schema.nspname, relation.relname
\gexec
REVOKE ALL ON DATABASE :"database" FROM PUBLIC, app CASCADE;
REVOKE ALL ON SCHEMA public FROM PUBLIC, app CASCADE;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, app CASCADE;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, app CASCADE;
REVOKE ALL ON ALL ROUTINES IN SCHEMA public FROM PUBLIC, app CASCADE;

GRANT CONNECT ON DATABASE :"database" TO app;
GRANT USAGE ON SCHEMA public TO app;
GRANT SELECT ON sources, django_migrations TO app;
GRANT UPDATE (enabled) ON sources TO app;
GRANT SELECT, INSERT ON ads, price_history TO app;
GRANT SELECT, INSERT, UPDATE ON properties TO app;
GRANT UPDATE (url, market_segment, description, is_active, observed_at, photos, photos_observed_at)
    ON ads TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON regions, property_types, auth_group,
    auth_group_permissions, auth_permission, auth_user, auth_user_groups,
    auth_user_user_permissions, django_admin_log, django_content_type, django_session TO app;
GRANT USAGE ON SEQUENCE ads_id_seq, price_history_id_seq, properties_id_seq,
    property_types_id_seq, regions_id_seq, auth_group_id_seq, auth_group_permissions_id_seq,
    auth_permission_id_seq, auth_user_id_seq, auth_user_groups_id_seq,
    auth_user_user_permissions_id_seq, django_admin_log_id_seq, django_content_type_id_seq TO app;
GRANT EXECUTE ON FUNCTION public.catalog_photos_valid(jsonb) TO app;
COMMIT;
SQL

docker compose exec -T -e DB_NAME -e DB_APP_PASSWORD db sh -ec '
    export PGREQUIREAUTH=scram-sha-256 PGDATABASE="$DB_NAME" PGPASSWORD="$DB_APP_PASSWORD"
    exec psql -Xq -h db -U app -v ON_ERROR_STOP=1 -c "SELECT 1"
' >/dev/null
