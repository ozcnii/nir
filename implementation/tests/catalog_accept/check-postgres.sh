#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh
if [[ ${1:-} != --leased ]]; then
    # Arrange
    export DB_NAME=t15_catalog_test
    export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
    export DB_APP_PASSWORD="$(openssl rand -hex 24)"
    export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
    export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
    reserve_disposable_postgres
    volume=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
    project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume")
    app_volume="${project}_app_data"
    owned_app_volume=false
    release_fixture() {
        for fixture in "$volume" "$app_volume"; do
            if [[ "$fixture" == "$app_volume" && "$owned_app_volume" == false ]]; then
                continue
            fi
            if [[ "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$fixture")" == "$NIR_PG_FIXTURE_TOKEN" ]]; then
                docker volume rm "$fixture" >/dev/null
            fi
        done
    }
    trap release_fixture EXIT
    if docker volume inspect "$app_volume" >/dev/null 2>&1; then
        echo "Catalog fixture refuses an existing app_data volume." >&2
        exit 1
    fi
    docker volume create --label "com.docker.compose.project=$project" \
        --label com.docker.compose.volume=app_data \
        --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$app_volume" >/dev/null
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$app_volume")" = "$NIR_PG_FIXTURE_TOKEN"
    owned_app_volume=true

    # Act
    uv run --locked python -m host_runner --db-only -- bash "$0" --leased

    # Assert
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    exit 0
fi

# Arrange: only this disposable database gets the fault-injection constraint.
assert_disposable_postgres
docker compose build app
bash scripts/prepare-db.sh
admin -d "$DB_NAME" -c "ALTER TABLE price_history ADD CONSTRAINT t15_fail_fixture CHECK (price_rub <> 12345)"

# Act / Assert: the Python cases verify actual app identity and persisted rows.
docker compose run --rm --no-deps -T -v "$PWD/tests/catalog_accept:/test-suite:ro" \
    -e PYTHONPATH=/app app python /test-suite/check_snapshot.py
