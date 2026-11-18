#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh

case "${1:-}" in
    --seed)
        # Arrange
        assert_disposable_postgres
        docker compose build app
        bash scripts/prepare-db.sh
        admin -d "$DB_NAME" -c "UPDATE regions SET name = 'Recovery probe retained' WHERE id = 68"

        # Act
        old=$(docker compose run -d --no-deps             -v "$PWD/tests/recovery:/test-suite:ro" -e PYTHONPATH=/app app             python /test-suite/check_runtime.py old-app)
        printf '%s\n' "$old" >"$T12_FIXTURE_DIR/old-container"
        docker exec "$old" python /test-suite/check_runtime.py identify >"$T12_FIXTURE_DIR/old-state"

        # Assert
        test "$(docker container inspect --format '{{.State.Running}}' "$old")" = true
        exit 0
        ;;
    --recovered)
        # Arrange
        app=$(docker compose ps -q app)
        read -r old_run old_instance good_run good_digest <"$T12_FIXTURE_DIR/old-state"

        # Act
        docker exec -i "$app" python - check "$old_run" "$old_instance" "$good_run" "$good_digest" <tests/recovery/check_runtime.py
        persisted=$(admin -d "$DB_NAME" -At -c 'SELECT name FROM public.regions WHERE id = 68')

        # Assert
        test "$persisted" = "Recovery probe retained"
        exit 0
        ;;
esac

# Arrange
export DB_NAME=t12_recovery_test
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export DB_APP_PASSWORD="$(openssl rand -hex 24)"
export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
export APP_PORT=0
export T12_FIXTURE_DIR="$(mktemp -d)"
reserve_disposable_postgres
db_volume=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$db_volume")
app_volume="${project}_app_data"
owned_app_volume=false
release_fixture() {
    if [[ -s "$T12_FIXTURE_DIR/old-container" ]]; then
        local old
        old=$(cat "$T12_FIXTURE_DIR/old-container")
        if [[ "$(docker container inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$old")" == "$project" ]]; then
            docker container rm --force "$old" >/dev/null
        fi
    fi
    if [[ "$owned_app_volume" == true ]] &&
        [[ "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$app_volume")" == "$NIR_PG_FIXTURE_TOKEN" ]]; then
        docker volume rm "$app_volume" >/dev/null
    fi
    if [[ "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$db_volume")" == "$NIR_PG_FIXTURE_TOKEN" ]]; then
        docker volume rm "$db_volume" >/dev/null
    fi
    for name in old-container old-state probe; do
        if [[ -f "$T12_FIXTURE_DIR/$name" ]]; then unlink "$T12_FIXTURE_DIR/$name"; fi
    done
    rmdir "$T12_FIXTURE_DIR"
}
trap release_fixture EXIT
if docker volume inspect "$app_volume" >/dev/null 2>&1; then
    echo "Recovery tests refuse an existing app_data volume." >&2
    exit 1
fi
docker volume create --label "com.docker.compose.project=$project" \
    --label com.docker.compose.volume=app_data \
    --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$app_volume" >/dev/null
owned_app_volume=true
uv run --locked python -m host_runner --db-only -- bash "$0" --seed
old=$(cat "$T12_FIXTURE_DIR/old-container")

# Act
if uv run --locked python -m host_runner -- true >"$T12_FIXTURE_DIR/probe" 2>&1; then
    echo "Recovery started while a previous app_data user was live." >&2
    exit 1
fi
if env -u DOCKER_CONTEXT DOCKER_HOST="unix://$T12_FIXTURE_DIR/unavailable.sock" \
    uv run --locked python -m host_runner -- true >>"$T12_FIXTURE_DIR/probe" 2>&1; then
    echo "Recovery started without a reachable Docker Engine." >&2
    exit 1
fi

# Assert
grep -F 'An application volume user has not stopped' "$T12_FIXTURE_DIR/probe"
grep -F 'unavailable.sock' "$T12_FIXTURE_DIR/probe"
test "$(docker container inspect --format '{{.State.Running}}' "$old")" = true

# Act
docker container kill --signal SIGKILL "$old" >/dev/null
test "$(docker container inspect --format '{{.State.Status}}' "$old")" = exited
uv run --locked python -m host_runner -- bash "$0" --recovered

# Assert
test -z "$(docker ps -q --filter "label=com.docker.compose.project=$project")"
for volume in "$db_volume" "$app_volume"; do
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" = "$NIR_PG_FIXTURE_TOKEN"
done
echo "T12: app and database volumes survived the managed restart."
