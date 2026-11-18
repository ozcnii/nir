#!/usr/bin/env bash

# Общие функции интеграционных проверок PostgreSQL из каталога implementation/.
reserve_disposable_postgres() {
    # Arrange
    if [[ ! -f ../.git ]]; then
        echo "PostgreSQL checks require a disposable Git worktree." >&2
        exit 1
    fi
    local project volume actual_token
    project=$(uv run --locked python -c \
        'import hashlib, os; print("nir-" + hashlib.sha256(os.fsencode(os.getcwd())).hexdigest()[:16])')
    volume="${project}_db"
    export NIR_PG_FIXTURE_TOKEN="$(openssl rand -hex 24)"
    # Act
    if docker volume inspect "$volume" >/dev/null 2>&1 ||
        [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]]; then
        echo "PostgreSQL checks refuse an existing project database volume or container." >&2
        exit 1
    fi
    docker volume create --label "com.docker.compose.project=$project" \
        --label com.docker.compose.volume=db \
        --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$volume" >/dev/null
    # Assert
    actual_token=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")
    test "$actual_token" = "$NIR_PG_FIXTURE_TOKEN"
}

run_disposable_postgres() {
    reserve_disposable_postgres
    exec uv run --locked python -m host_runner --db-only -- "$@"
}

run_disposable_postgres_with_cleanup() (
    # Arrange
    export DB_NAME="$1"
    shift
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
    uv run --locked python -m host_runner --db-only -- "$@"

    # Assert
    project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume")
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" = "$NIR_PG_FIXTURE_TOKEN"
)

admin() {
    docker compose exec -T db sh -c \
        'PGREQUIREAUTH=scram-sha-256 PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h db -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"' sh "$@"
}

assert_disposable_postgres() {
    # Arrange
    : "${COMPOSE_PROJECT_NAME:?Run with the disposable test launcher}"
    : "${NIR_PG_FIXTURE_TOKEN:?Run with the disposable test launcher}"
    if [[ "${NIR_RUNNER_GENERATION:-unmanaged}" == unmanaged ]]; then
        echo "PostgreSQL checks require an owned, disposable PostgreSQL volume." >&2
        exit 1
    fi
    # Act
    local container mount token cluster_state
    container=$(docker compose ps -q db)
    mount=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$container")
    token=$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$mount")
    cluster_state=$(admin -At -c "SELECT
        (SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')),
        (SELECT count(*) FROM pg_roles WHERE rolname IN ('app','migrator'))")

    # Assert
    test "$mount" = "${COMPOSE_PROJECT_NAME}_db"
    test "$token" = "$NIR_PG_FIXTURE_TOKEN"
    test "$cluster_state" = "0|0"
}
