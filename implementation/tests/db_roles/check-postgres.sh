#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ ${1:-} != --leased ]]; then
    # Arrange
    if [[ ! -f ../.git ]]; then
        echo "ACL checks require a disposable Git worktree." >&2
        exit 1
    fi
    project=$(uv run --locked python -c \
        'import hashlib, os; print("nir-" + hashlib.sha256(os.fsencode(os.getcwd())).hexdigest()[:16])')
    volume="${project}_db"
    export NIR_T06_FIXTURE_TOKEN="$(openssl rand -hex 24)"
    # Act
    if docker volume inspect "$volume" >/dev/null 2>&1 ||
        [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]]; then
        echo "ACL checks refuse an existing project database volume or container." >&2
        exit 1
    fi
    docker volume create --label "com.docker.compose.project=$project" \
        --label com.docker.compose.volume=db \
        --label "org.nir.fixture.token=$NIR_T06_FIXTURE_TOKEN" "$volume" >/dev/null
    # Assert
    actual_token=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")
    test "$actual_token" = "$NIR_T06_FIXTURE_TOKEN"
    exec uv run --locked python -m host_runner --db-only -- bash "$0" --leased
fi

# Arrange
: "${COMPOSE_PROJECT_NAME:?Run with the disposable test launcher}"
: "${NIR_T06_FIXTURE_TOKEN:?Run with the disposable test launcher}"
if [[ "${NIR_RUNNER_GENERATION:-unmanaged}" == unmanaged ]]; then
    echo "ACL checks require an owned, disposable PostgreSQL volume." >&2
    exit 1
fi
export DB_NAME=t06_acl_test

admin() {
    docker compose exec -T db sh -c \
        'PGREQUIREAUTH=scram-sha-256 PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h db -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"' sh "$@"
}
runtime_check() {
    docker compose run --rm --no-deps -T app python -m config.db_roles check
}
f25() {
    docker compose run --rm --no-deps -T -e DB_MIGRATOR_PASSWORD -e NIR_T06_FIXTURE_TOKEN \
        --volume "$PWD/tests/db_roles/check_postgres.py:/tmp/check_postgres.py:ro" \
        app python /tmp/check_postgres.py "$@"
}

# Act
container=$(docker compose ps -q db)
mount=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$container")
token=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$mount")
cluster_state=$(admin -At -c "SELECT
    (SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')),
    (SELECT count(*) FROM pg_roles WHERE rolname IN ('app','migrator'))")

# Assert
test "$mount" = "${COMPOSE_PROJECT_NAME}_db"
test "$token" = "$NIR_T06_FIXTURE_TOKEN"
test "$cluster_state" = "0|0"

# Arrange
guard_log=$(mktemp)
trap 'rm -f "$guard_log"' EXIT
# Act
if NIR_RUNNER_GENERATION=unmanaged PATH=/usr/bin:/bin \
    /bin/bash scripts/prepare-db.sh >"$guard_log" 2>&1; then
    echo "Unexpected preparation under an unmanaged lease" >&2
    exit 1
fi
# Assert
grep -F "requires a managed host_runner lease" "$guard_log"

# Arrange
admin -c "ALTER SYSTEM SET log_statement = 'all'"
admin -c "ALTER SYSTEM SET log_min_duration_statement = 0"
admin -c 'SELECT pg_reload_conf()'
docker compose build app
test "$(admin -At -c 'SHOW log_statement')" = all
test "$(admin -At -c 'SHOW log_min_duration_statement')" = 0
admin -d template1 -c 'CREATE TABLE public.fixture_template1 (id integer)'

# Act
bash scripts/prepare-db.sh
admin -c "SELECT 't06_statement_logging_is_active'" >/dev/null
logs=$(docker compose logs --no-color db)

# Assert
test "$(admin -d "$DB_NAME" -At -c "SELECT to_regclass('public.fixture_template1') IS NULL")" = t
admin -d template1 -c 'DROP TABLE public.fixture_template1'
echo "New catalog did not copy local template1 objects"
grep -qF "t06_statement_logging_is_active" <<<"$logs"
case "$logs" in
    *"$DB_APP_PASSWORD"*|*"$DB_MIGRATOR_PASSWORD"*|*"$POSTGRES_PASSWORD"*|*'SCRAM-SHA-256$'*)
        echo "Database logs disclosed a password or SCRAM verifier" >&2
        exit 1
        ;;
esac
echo "Full PostgreSQL statement/duration logging showed neither passwords nor verifiers"

# Arrange
runtime_check
# Act
if f25 >"$guard_log" 2>&1; then
    echo "F25 unexpectedly accepted an unmarked database" >&2
    exit 1
fi
# Assert
grep -F "F25 requires the marked disposable database and app login" "$guard_log"
test "$(admin -d "$DB_NAME" -At -c 'SELECT count(*) FROM sources')" = 0

# Arrange
admin -c "COMMENT ON DATABASE t06_acl_test IS 'nir-f25:$NIR_T06_FIXTURE_TOKEN'"
# Act
f25
# Assert
test "$(admin -d "$DB_NAME" -At -c 'SELECT count(*) FROM price_history')" = 1

# Arrange
passwords_before=$(admin -At -c \
    "SELECT rolpassword FROM pg_authid WHERE rolname IN ('app', 'migrator') ORDER BY rolname")
test -n "$passwords_before"
admin -d "$DB_NAME" <<'SQL'
GRANT UPDATE (price_rub) ON price_history TO app WITH GRANT OPTION;
GRANT UPDATE (price_kind) ON price_history TO PUBLIC;
GRANT UPDATE ON SEQUENCE price_history_id_seq TO app;
CREATE FUNCTION public.fixture_definer() RETURNS integer
    LANGUAGE sql SECURITY DEFINER RETURN 1;
ALTER FUNCTION public.fixture_definer() OWNER TO migrator;
SQL

# Act
if runtime_check; then
    echo "Runtime accepted a divergent ACL" >&2
    exit 1
fi
bash scripts/prepare-db.sh
runtime_check
f25 repeat
passwords_after=$(admin -At -c \
    "SELECT rolpassword FROM pg_authid WHERE rolname IN ('app', 'migrator') ORDER BY rolname")

# Assert
test "$passwords_before" = "$passwords_after"
echo "Repeat preparation kept data and both role password verifiers"

# Arrange
admin -c 'CREATE DATABASE t06_wrong_owner OWNER postgres'
# Act
if DB_NAME=t06_wrong_owner bash scripts/prepare-db.sh >"$guard_log" 2>&1; then
    echo "Unexpected preparation over a foreign database owner" >&2
    exit 1
fi
# Assert
grep -F "conflicts with the expected migrator ownership" "$guard_log"
test "$(admin -At -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='t06_wrong_owner'")" = postgres
test "$(admin -d t06_wrong_owner -At -c "SELECT to_regclass('public.django_migrations') IS NULL")" = t

# Arrange
conflict=t06_unmigrated
admin -c "CREATE DATABASE $conflict OWNER migrator"
for kind in schema owner app-password migrator-password; do
    # Arrange
    case "$kind" in
        schema)
            admin -d "$conflict" -c "CREATE SCHEMA fixture_schema AUTHORIZATION app"
            expected="Unexpected schema fixture_schema"
            ;;
        owner)
            admin -d "$conflict" -c "CREATE TABLE public.fixture_owner (id integer)"
            expected="Unexpected owner of TABLE fixture_owner"
            ;;
        app-password) expected='password authentication failed for user "app"' ;;
        migrator-password) expected='password authentication failed for user "migrator"' ;;
    esac

    # Act
    if (
        export DB_NAME="$conflict"
        case "$kind" in
            app-password) export DB_APP_PASSWORD=incorrect-fixture-value ;;
            migrator-password) export DB_MIGRATOR_PASSWORD=incorrect-fixture-value ;;
        esac
        bash scripts/prepare-db.sh
    ) >"$guard_log" 2>&1; then
        echo "Unexpected preparation over conflict $kind" >&2
        exit 1
    fi

    # Assert
    grep -F "$expected" "$guard_log"
    test "$(admin -d "$conflict" -At -c "SELECT to_regclass('public.django_migrations') IS NULL")" = t
    case "$kind" in
        schema) admin -d "$conflict" -c "DROP SCHEMA fixture_schema" ;;
        owner) admin -d "$conflict" -c "DROP TABLE public.fixture_owner" ;;
    esac
done
echo "Ownership, unexpected schema and both credential conflicts stopped before migrations"

# Arrange
admin -d "$conflict" <<'SQL'
SET ROLE migrator;
CREATE TABLE public.ads (id integer);
CREATE FUNCTION public.fixture_return_new() RETURNS trigger
    LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END;';
CREATE TRIGGER fixture_trigger BEFORE INSERT ON public.ads
    FOR EACH ROW EXECUTE FUNCTION public.fixture_return_new();
SQL
# Act
if DB_NAME="$conflict" bash scripts/prepare-db.sh >"$guard_log" 2>&1; then
    echo "Database preparation accepted a custom trigger" >&2
    exit 1
fi
# Assert
grep -F "Unexpected trigger fixture_trigger on app-writable table ads" "$guard_log"
test "$(admin -d "$conflict" -At -c "SELECT to_regclass('public.django_migrations') IS NULL")" = t

# Arrange
runtime_check
admin -d "$DB_NAME" <<'SQL'
SET ROLE migrator;
CREATE FUNCTION public.fixture_return_new() RETURNS trigger
    LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END;';
REVOKE EXECUTE ON FUNCTION public.fixture_return_new() FROM PUBLIC;
CREATE TRIGGER fixture_trigger BEFORE INSERT ON public.ads
    FOR EACH ROW EXECUTE FUNCTION public.fixture_return_new();
SQL
# Act
if runtime_check >"$guard_log" 2>&1; then
    echo "Runtime preflight accepted a custom trigger" >&2
    exit 1
fi
# Assert
grep -F "Unexpected trigger fixture_trigger on app-writable table ads" "$guard_log"

# Arrange
admin -d "$DB_NAME" <<'SQL'
SET ROLE migrator;
DROP TRIGGER fixture_trigger ON public.ads;
DROP FUNCTION public.fixture_return_new();
CREATE RULE fixture_rule AS ON INSERT TO public.ads DO ALSO NOTHING;
SQL
# Act
if runtime_check >"$guard_log" 2>&1; then
    echo "Runtime preflight accepted a rewrite rule" >&2
    exit 1
fi
# Assert
grep -F "Unexpected rewrite rule fixture_rule on app-writable table ads" "$guard_log"
admin -d "$DB_NAME" -c 'DROP RULE fixture_rule ON public.ads'
runtime_check

# Arrange
fixture_volume="${COMPOSE_PROJECT_NAME}_db"
original_token=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$fixture_volume")
# Act
if bash "$0" >"$guard_log" 2>&1; then
    echo "Fixture launcher unexpectedly reused its volume" >&2
    exit 1
fi
# Assert
grep -F "refuse an existing project database volume or container" "$guard_log"
test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$fixture_volume")" = "$original_token"
