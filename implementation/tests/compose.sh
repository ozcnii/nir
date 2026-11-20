#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Arrange: own a disposable project and database volume for this test only.
export COMPOSE_PROJECT_NAME="nir-check-$(openssl rand -hex 12)"
export COMPOSE_FILE="$PWD/compose.yaml"
export COMPOSE_DISABLE_ENV_FILE=1
export APP_PORT=0
export DB_NAME=t15_catalog_test
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export DB_APP_PASSWORD="$(openssl rand -hex 24)"
export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
export COLLECTION_PROFILE=acceptance
export NIR_PG_FIXTURE_TOKEN="$(openssl rand -hex 24)"

project_label="com.docker.compose.project=$COMPOSE_PROJECT_NAME"
volume="${COMPOSE_PROJECT_NAME}_db"
volume_token() {
    docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume"
}
if docker volume inspect "$volume" >/dev/null 2>&1 ||
    [[ -n "$(docker ps -aq --filter "label=$project_label")" ]] ||
    [[ -n "$(docker network ls -q --filter "label=$project_label")" ]]; then
    echo "The disposable test project already exists" >&2
    exit 1
fi
docker volume create --label "$project_label" --label com.docker.compose.volume=db \
    --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$volume" >/dev/null
[[ "$(volume_token)" == "$NIR_PG_FIXTURE_TOKEN" ]]
cleanup() {
    [[ "$(volume_token)" == "$NIR_PG_FIXTURE_TOKEN" ]] || return 1
    docker compose down --remove-orphans --rmi local
    docker volume rm "$volume" >/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

admin() {
    docker compose exec -T db psql -X -U postgres -v ON_ERROR_STOP=1 "$@"
}

test_mount=(-v "$PWD/tests:/test-suite:ro" -e PYTHONPATH=/app:/test-suite -e NIR_PG_FIXTURE_TOKEN)
check() {
    local script=$1
    shift
    docker compose run --rm --no-deps -T "${test_mount[@]}" app python "/test-suite/$script" "$@"
}

# Act: prepare PostgreSQL and start Django using the project commands.
docker compose build app
docker compose up --wait db
bash tools/prepare-db.sh
docker compose up --wait app

# Assert: check the real HTTP route and read/write as the restricted app role.
curl --fail --silent --show-error --max-time 5 \
    "http://$(docker compose port app 8000)/admin/login/" >/dev/null
docker compose run --rm --no-deps -T -e POSTGRES_PASSWORD \
    -e DJANGO_SETTINGS_MODULE=app.config.settings_test app sh -ec '
    python -m app.manage check
    python -m app.manage makemigrations --check --dry-run
    python -m app.manage test
'
admin -d "$DB_NAME" <<SQL
COMMENT ON DATABASE $DB_NAME IS 'nir-f25:$NIR_PG_FIXTURE_TOKEN';
INSERT INTO sources VALUES ('fixture', true);
ALTER TABLE price_history ADD CONSTRAINT t15_fail_fixture CHECK (price_rub <> 12345);
SQL
check db_roles/check_postgres.py
check catalog_accept/check_snapshot.py
check first_pass/check_first_pass.py

# Arrange / Act: wait for the command to enter its browser context, then stop it.
collector=$(docker compose run -d --no-deps "${test_mount[@]}" \
    app python /test-suite/first_pass/check_first_pass.py stop)
for ((attempt = 0; attempt < 100; attempt++)); do
    if docker logs "$collector" 2>&1 | grep -Fq 'Collect command ready for SIGTERM'; then
        break
    fi
    sleep 0.2
done
[[ "$attempt" -lt 100 ]]
check first_pass/check_first_pass.py busy
docker container stop --time 10 "$collector" >/dev/null

# Assert: the command handles SIGTERM and exits without aborting Django.
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$collector")" == 0 ]]
docker logs "$collector" 2>&1 | grep -F 'Collection stopped.'
docker container rm "$collector" >/dev/null
check first_pass/check_first_pass.py unlocked
curl --fail --silent --show-error --max-time 5 \
    "http://$(docker compose port app 8000)/admin/login/" >/dev/null
