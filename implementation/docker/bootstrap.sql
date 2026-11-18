\getenv target_db DB_NAME
\getenv migrator_password DB_MIGRATOR_PASSWORD
\getenv app_password DB_APP_PASSWORD

SELECT pg_catalog.set_config('nir.bootstrap_db', :'target_db', false) AS target_db \gset
-- The lock lives in the maintenance database across CREATE DATABASE's commit.
SELECT pg_catalog.pg_advisory_lock(1981687401, 1) AS locked \gset

DO $bootstrap$
DECLARE
    target text := current_setting('nir.bootstrap_db');
BEGIN
    IF current_user <> 'postgres' OR current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'Bootstrap must connect as postgres to the maintenance database';
    END IF;
    IF target IN ('', 'postgres', 'template0', 'template1')
       OR octet_length(target) > current_setting('max_identifier_length')::integer THEN
        RAISE EXCEPTION 'DB_NAME must name a dedicated application database';
    END IF;
    IF EXISTS (
        SELECT FROM pg_database
        WHERE datname = target
          AND (pg_get_userbyid(datdba) <> 'migrator' OR datistemplate OR NOT datallowconn)
    ) THEN
        RAISE EXCEPTION 'Database % conflicts with the expected migrator ownership or attributes', target;
    END IF;
    IF EXISTS (
        SELECT FROM pg_roles
        WHERE rolname IN ('migrator', 'app')
          AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
               OR rolreplication OR rolbypassrls
               OR oid IN (SELECT member FROM pg_auth_members))
    ) THEN
        RAISE EXCEPTION 'Existing migrator or app role has conflicting attributes or memberships';
    END IF;
END
$bootstrap$;

BEGIN;
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    role_name, role_password
)
FROM (VALUES ('migrator', :'migrator_password'), ('app', :'app_password'))
    AS requested(role_name, role_password)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name)
\gexec
COMMIT;

SELECT format('CREATE DATABASE %I OWNER migrator', :'target_db')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'target_db')
\gexec
