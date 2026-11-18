#!/usr/bin/env bash
set -euo pipefail

# Run under host_runner in a fresh, disposable test worktree.
cd "$(dirname "$0")/../.."
: "${COMPOSE_PROJECT_NAME:?Run under host_runner --db-only in a test worktree}"
if [[ "${NIR_RUNNER_GENERATION:-unmanaged}" == unmanaged ]]; then
    echo "ACL checks require an owned, disposable PostgreSQL volume." >&2
    exit 1
fi
export DB_NAME=t06_acl_test

admin() {
    docker compose exec -T db sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h db -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"' sh "$@"
}
runtime_check() {
    docker compose run --rm --no-deps -T app python -m config.db_roles check
}
f25() {
    docker compose run --rm --no-deps -T -e DB_MIGRATOR_PASSWORD \
        --volume "$PWD/tests/db_roles/check_postgres.py:/tmp/check_postgres.py:ro" \
        app python /tmp/check_postgres.py "$@"
}

# An unmanaged lease must fail before reaching Docker.
guard_log=$(mktemp)
trap 'rm -f "$guard_log"' EXIT
if NIR_RUNNER_GENERATION=unmanaged PATH=/usr/bin:/bin \
    /bin/bash scripts/prepare-db.sh >"$guard_log" 2>&1; then
    echo "Unexpected preparation under an unmanaged lease" >&2
    exit 1
fi
grep -F "requires a managed host_runner lease" "$guard_log"
docker compose build app
bash scripts/prepare-db.sh
runtime_check
f25

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
if runtime_check; then
    echo "Runtime accepted a divergent ACL" >&2
    exit 1
fi
bash scripts/prepare-db.sh
runtime_check
f25 repeat
passwords_after=$(admin -At -c \
    "SELECT rolpassword FROM pg_authid WHERE rolname IN ('app', 'migrator') ORDER BY rolname")
test "$passwords_before" = "$passwords_after"
echo "Repeat preparation kept data and both role password verifiers"

admin -c 'CREATE DATABASE t06_wrong_owner OWNER postgres'
if DB_NAME=t06_wrong_owner bash scripts/prepare-db.sh >"$guard_log" 2>&1; then
    echo "Unexpected preparation over a foreign database owner" >&2
    exit 1
fi
grep -F "conflicts with the expected migrator ownership" "$guard_log"
test "$(admin -At -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='t06_wrong_owner'")" = postgres
test "$(admin -d t06_wrong_owner -At -c "SELECT to_regclass('public.django_migrations') IS NULL")" = t
echo "Wrong owner rejected before migrations and ownership preserved"
