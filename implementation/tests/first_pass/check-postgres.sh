#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh
if [[ ${1:-} != --leased && ${1:-} != --persisted ]]; then
    # Arrange
    mode=${1:-fixture}
    case "$mode" in fixture|live) ;; *) exit 2 ;; esac
    export DB_NAME=t16_first_pass
    export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
    export DB_APP_PASSWORD="$(openssl rand -hex 24)"
    export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
    export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
    reserve_disposable_postgres
    database=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
    project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$database")
    app_volume="${project}_app_data"
    owned_app_volume=false
    release_volumes() {
        for volume in "$database" "$app_volume"; do
            if [[ "$volume" == "$app_volume" && "$owned_app_volume" == false ]]; then
                continue
            fi
            if [[ "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" == "$NIR_PG_FIXTURE_TOKEN" ]]; then
                docker volume rm "$volume" >/dev/null
            fi
        done
    }
    trap release_volumes EXIT
    if docker volume inspect "$app_volume" >/dev/null 2>&1; then
        echo "First-pass fixture refuses an existing app_data volume." >&2
        exit 1
    fi
    docker volume create --label "com.docker.compose.project=$project" \
        --label com.docker.compose.volume=app_data \
        --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$app_volume" >/dev/null
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$app_volume")" = "$NIR_PG_FIXTURE_TOKEN"
    owned_app_volume=true

    # Act
    result=0
    uv run --locked python -m host_runner --db-only -- bash "$0" --leased "$mode" || result=$?

    # Assert: no fixtures are still using the volumes. Reopen the database after
    # successful release to check the committed rows are on its preserved volume.
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    if [[ "$result" = 0 ]]; then
        uv run --locked python -m host_runner --db-only -- bash "$0" --persisted
        test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    fi
    exit "$result"
fi

if [[ $1 == --persisted ]]; then
    # Arrange: the host runner restored the same database volume.
    test -n "$NIR_PG_FIXTURE_TOKEN"

    # Act
    persisted=$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM ads WHERE source_code='cian' AND external_id='330631159'")

    # Assert
    test "$persisted" = 1
    echo 'Committed publication survived app and database release.'
    exit 0
fi

# Arrange
assert_disposable_postgres
docker compose build app
bash scripts/prepare-db.sh
app=$(docker compose run -d --no-deps \
    --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" \
    -v "$PWD/tests/first_pass:/test-suite:ro" -e PYTHONPATH=/app \
    app python /test-suite/check_runtime.py launch "$2")
release_app() {
    local label
    label=$(docker container inspect --format '{{index .Config.Labels "org.nir.fixture.token"}}' "$app")
    test "$label" = "$NIR_PG_FIXTURE_TOKEN"
    docker container stop --time 60 "$app" >/dev/null
    exit_code=$(docker container inspect --format '{{.State.ExitCode}}' "$app")
    docker container rm "$app" >/dev/null
    test "$exit_code" = 0
}
trap release_app EXIT

# Act
result=0
docker exec "$app" python /test-suite/check_runtime.py verify "$2" || result=$?

# Assert
trap - EXIT
release_app
admin -d "$DB_NAME" -At -c "SELECT
  (SELECT count(*) FROM properties),
  (SELECT count(*) FROM ads),
  (SELECT count(*) FROM price_history)"
exit "$result"
