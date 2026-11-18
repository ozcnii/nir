#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh

if [[ ${1:-} != --leased ]]; then
    # Arrange
    export DB_NAME=t09_launcher_test
    export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
    export DB_APP_PASSWORD="$(openssl rand -hex 24)"
    export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
    export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
    reserve_disposable_postgres
    volume=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
    release_fixture() {
        local label
        label=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")
        if [[ "$label" == "$NIR_PG_FIXTURE_TOKEN" ]]; then
            docker volume rm "$volume" >/dev/null
        fi
    }
    trap release_fixture EXIT

    # Act
    uv run --locked python -m host_runner --db-only -- bash "$0" --leased

    # Assert
    project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume")
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" = "$NIR_PG_FIXTURE_TOKEN"
    exit 0
fi

# Arrange
assert_disposable_postgres
docker compose build app
bash scripts/prepare-db.sh
admin -d "$DB_NAME" -c '
    CREATE TABLE public.t09_probe (id integer PRIMARY KEY);
    GRANT INSERT, SELECT ON public.t09_probe TO app;
'
# The normal invocation uses Compose's tracked profile. A disposable override
# permits controlled evidence while reviewing PIDFD support in the pinned profile.
compose_args=(-f compose.yaml)
if [[ -n ${NIR_TEST_SECCOMP_PATH:-} ]]; then
    override=$(mktemp)
    trap 'rm -f "$override"' EXIT
    printf 'services:\n  app:\n    security_opt: !override ["seccomp=%s"]\n' "$NIR_TEST_SECCOMP_PATH" >"$override"
    compose_args+=(-f "$override")
fi

# Act
suite_result=0
docker compose "${compose_args[@]}" run --rm --no-deps -T \
    -v "$PWD/tests/launcher:/test-suite:ro" \
    -e PYTHONPATH=/app app python /test-suite/check_launcher_postgres.py || suite_result=$?

# Assert
test "$suite_result" = 0
