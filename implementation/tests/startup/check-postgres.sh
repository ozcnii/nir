#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh
export DB_NAME=t08_startup_test
if [[ ${1:-} != --leased ]]; then
    # Arrange
    reserve_disposable_postgres
    uv run --locked python -m host_runner --db-only -- bash "$0" --leased
    volume=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
    project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume")
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    probe=$(mktemp -d)
    trap 'rm -f "$probe/runner.log" "$probe/task-started"; rmdir "$probe"' EXIT

    # Act
    if uv run --locked python -m host_runner -- touch "$probe/task-started" >"$probe/runner.log" 2>&1; then
        echo "Host runner reported ready with a missing application grant" >&2
        exit 1
    fi

    # Assert
    grep -F 'Runtime preflight failed: Database roles: ACL mismatch.' "$probe/runner.log"
    grep -qF "'ads', None), 'app', 'INSERT'" "$probe/runner.log"
    if grep -qE '^http://127[.]0[.]0[.]1:[0-9]+$' "$probe/runner.log" || [[ -e "$probe/task-started" ]]; then
        echo "Host runner announced an address or started the task after preflight failure" >&2
        exit 1
    fi
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" = "$NIR_PG_FIXTURE_TOKEN"
    echo "Host runner reported the failed preflight, preserved the test volume and released its containers."
    exit 0
fi
assert_disposable_postgres

check_runtime() {
    docker compose run --rm --no-deps -T app sh -ec '
        test -z "${POSTGRES_PASSWORD+x}${DB_MIGRATOR_PASSWORD+x}"
        exec python -m config.startup --check-only
    '
}

# Arrange
test_log=$(mktemp)
trap 'rm -f "$test_log"' EXIT
docker compose build app
docker compose run --rm --no-deps -T -e POSTGRES_PASSWORD -e DB_MIGRATOR_PASSWORD \
    -e DJANGO_SECRET_KEY= app python -m config.db_bootstrap
# Act
if check_runtime >"$test_log" 2>&1; then
    echo "Runtime accepted an unmigrated database" >&2
    exit 1
fi
# Assert
grep -F "Runtime preflight failed: unapplied Django migrations." "$test_log"
test "$(admin -d "$DB_NAME" -At -c "SELECT to_regclass('public.django_migrations') IS NULL")" = t

# Arrange
admin -d "$DB_NAME" -c 'SET ROLE migrator; CREATE TABLE public.regions (id integer)'
# Act
if bash scripts/prepare-db.sh >"$test_log" 2>&1; then
    echo "Database preparation accepted a conflicting relation" >&2
    exit 1
fi
# Assert
grep -F 'relation "regions" already exists' "$test_log"
test "$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM django_migrations WHERE app='catalog'")" = 0

# Arrange
test "$(admin -d "$DB_NAME" -At -c "SELECT to_regclass('public.sources') IS NULL")" = t
# Act
if check_runtime >"$test_log" 2>&1; then
    echo "Runtime reported ready after a failed migration" >&2
    exit 1
fi
# Assert
grep -F 'Runtime preflight failed: migration check failed:' "$test_log"
grep -F 'permission denied for table django_migrations' "$test_log"
test "$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM django_migrations WHERE app='catalog'")" = 0

# Arrange
admin -d "$DB_NAME" -c 'DROP TABLE public.regions'
export DJANGO_SUPERUSER_USERNAME=t08_operator
export DJANGO_SUPERUSER_PASSWORD="$(openssl rand -hex 24)"
# Act
bash scripts/prepare-db.sh
docker compose run --rm --no-deps -T -e DJANGO_SUPERUSER_USERNAME -e DJANGO_SUPERUSER_PASSWORD \
    app python manage.py createsuperuser --noinput --email ''
check_runtime
# Assert
test "$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM regions")" = 89
test "$(admin -d "$DB_NAME" -At -c "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.regions'::regclass")" = migrator
test "$(admin -d "$DB_NAME" -At -c "SELECT is_superuser FROM auth_user WHERE username='t08_operator'")" = t
unset DJANGO_SUPERUSER_PASSWORD

# Arrange
invalid_app_password=invalid-acceptance-fixture-password
# Act
if DB_APP_PASSWORD="$invalid_app_password" check_runtime >"$test_log" 2>&1; then
    echo "Runtime accepted invalid app credentials" >&2
    exit 1
fi
# Assert
grep -F "Runtime preflight failed: migration check failed:" "$test_log"
grep -F 'password authentication failed for user "app"' "$test_log"

# Arrange
admin -d "$DB_NAME" -c 'REVOKE INSERT ON public.ads FROM app'
# Act
if check_runtime >"$test_log" 2>&1; then
    echo "Runtime accepted a missing application grant" >&2
    exit 1
fi
# Assert
grep -F 'Runtime preflight failed: Database roles: ACL mismatch.' "$test_log"
grep -F "'ads', None), 'app', 'INSERT'" "$test_log"

# Arrange
docker compose run --rm --no-deps -T app python manage.py shell -c '
from catalog.models import PropertyType, Region, Source

Region.objects.filter(pk=68).update(name="Renamed region")
PropertyType.objects.filter(pk=1).update(name="Renamed type")
Source.objects.filter(pk="cian").update(enabled=False)
'
before=$(admin -d "$DB_NAME" -At -c "
    SELECT (SELECT name FROM regions WHERE id=68),
           (SELECT name FROM property_types WHERE id=1),
           (SELECT enabled FROM sources WHERE code='cian')")
test "$before" = 'Renamed region|Renamed type|f'
password_before=$(admin -d "$DB_NAME" -At -c "SELECT password FROM auth_user WHERE username='t08_operator'")
test -n "$password_before"
# Act
bash scripts/prepare-db.sh
check_runtime
check_runtime
# Assert
after=$(admin -d "$DB_NAME" -At -c "
    SELECT (SELECT name FROM regions WHERE id=68),
           (SELECT name FROM property_types WHERE id=1),
           (SELECT enabled FROM sources WHERE code='cian')")
password_after=$(admin -d "$DB_NAME" -At -c "SELECT password FROM auth_user WHERE username='t08_operator'")
test "$before" = "$after"
test "$password_before" = "$password_after"
test "$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM auth_user")" = 1
echo "Repeat preparation and runtime checks preserved reference edits, enabled flag and site administrator."

# Arrange the isolated database for the managed app startup after this lease ends.
admin -d "$DB_NAME" -c 'REVOKE INSERT ON public.ads FROM app'
