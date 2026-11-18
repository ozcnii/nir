#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh
script="$PWD/tests/stack/check-postgres.sh"

case "${1:-}" in
    --prepare)
        assert_disposable_postgres
        docker compose build app
        bash scripts/prepare-db.sh
        admin -d "$DB_NAME" -c "UPDATE regions SET name = 'Managed runtime probe' WHERE id = 68"
        exit 0
        ;;
    --hold)
        printf '%s %s\n' "$NIR_APP_URL" "$NIR_RUNNER_GENERATION" >"$2.tmp"
        mv "$2.tmp" "$2"
        while [[ ! -e "$3" ]]; do sleep .1; done
        exit 0
        ;;
esac

# Arrange a dedicated Compose project and one deliberate port owner.
export DB_NAME=t13_stack_test
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export DB_APP_PASSWORD="$(openssl rand -hex 24)"
export DB_MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
export DJANGO_SECRET_KEY="$(openssl rand -hex 32)"
unset APP_PORT
python=$(uv run --locked python -c 'import sys; print(sys.executable)')
probe=$(mktemp -d)
project=
db_volume=
app_volume=
port_owner=
first=
second=
cleanup() {
    for pid in "$first" "$second" "$port_owner"; do
        if [[ -n "$pid" ]]; then
            kill -TERM "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    for volume in "$app_volume" "$db_volume"; do
        if [[ -n "$volume" ]] &&
            [[ "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" == "${NIR_PG_FIXTURE_TOKEN:-}" ]]; then
            docker volume rm "$volume" >/dev/null
        fi
    done
    for file in first first.tmp first.log release-first second second.tmp second.log \
        restart restart.tmp restart.log release-restart port.log foreign/marker; do
        if [[ -f "$probe/$file" ]]; then unlink "$probe/$file"; fi
    done
    rmdir "$probe/foreign" "$probe"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$probe/foreign"
printf 'port-owner-%s\n' "$(openssl rand -hex 12)" >"$probe/foreign/marker"
"$python" -m http.server 8000 --bind 127.0.0.1 --directory "$probe/foreign" >"$probe/port.log" 2>&1 &
port_owner=$!
for ((attempt = 0; attempt < 100; attempt++)); do
    if curl -fsS --max-time 1 http://127.0.0.1:8000/marker 2>/dev/null |
        cmp -s "$probe/foreign/marker" -; then break; fi
    if ! kill -0 "$port_owner" 2>/dev/null; then
        cat "$probe/port.log" >&2
        exit 1
    fi
    sleep .1
done
curl -fsS --max-time 2 http://127.0.0.1:8000/marker | cmp -s "$probe/foreign/marker" -

reserve_disposable_postgres
db_volume=$(docker volume ls --filter "label=org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" --format '{{.Name}}')
project=$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$db_volume")
candidate="${project}_app_data"
if docker volume inspect "$candidate" >/dev/null 2>&1; then
    echo 'Managed runtime check refuses an existing app_data volume.' >&2
    exit 1
fi
app_volume=$(docker volume create --label "com.docker.compose.project=$project" \
    --label com.docker.compose.volume=app_data \
    --label "org.nir.fixture.token=$NIR_PG_FIXTURE_TOKEN" "$candidate")
"$python" -m host_runner --db-only -- bash "$script" --prepare

await_task() {
    local name=$1 pid=$2 attempt
    for ((attempt = 0; attempt < 600; attempt++)); do
        if [[ -s "$probe/$name" ]]; then return; fi
        if ! kill -0 "$pid" 2>/dev/null; then break; fi
        sleep .1
    done
    echo "The managed $name lease did not reach the task." >&2
    cat "$probe/$name.log" >&2
    return 1
}

container_id() {
    local id
    id=$(docker compose --project-name "$project" ps --status running --quiet "$1")
    docker container inspect --format '{{.Id}}' "$id"
}

assert_app() {
    local app_id=$1 address=$2 generation=$3
    test "$(docker container inspect --format '{{.Config.User}} {{.State.Health.Status}}' "$app_id")" = '10001:10001 healthy'
    test "$(docker container inspect --format '{{index .Config.Labels "org.nir.runner.generation"}}' "$app_id")" = "$generation"
    docker logs "$app_id" 2>&1 | grep -Fx 'Runtime preflight: database is ready.'
    curl -fsS -o /dev/null --max-time 3 "$address/admin/login/"
    # Compare runtime versions and login, and confirm startup did not start collection.
    docker exec -i "$app_id" python - <<'PYCODE'
import os
import sys

import django
from django.db import connection
from importlib.metadata import version

from config.control import request

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()
assert sys.version_info[:3] == (3, 13, 18)
assert django.get_version() == "5.2.19"
assert version("psycopg") == "3.3.9"
assert version("playwright") == "1.65.2"
with connection.cursor() as cursor:
    cursor.execute("SELECT current_user, session_user, current_setting('server_version_num'), (SELECT name FROM regions WHERE id = 68), (SELECT count(*) FROM ads)")
    assert cursor.fetchone() == ("app", "app", "170012", "Managed runtime probe", 0)
state = request({"action": "status"})
assert state["state"] == "idle" and state["run_id"] is None
print("Runtime Python 3.13.18 / Django 5.2.19 / psycopg 3.3.9 / Playwright 1.65.2 / PostgreSQL 17.12; app role; collection idle.")
PYCODE
}

assert_volumes() {
    for volume in "$db_volume" "$app_volume"; do
        test "$(docker volume inspect --format '{{index .Labels "org.nir.fixture.token"}}' "$volume")" = "$NIR_PG_FIXTURE_TOKEN"
    done
}

# Act: start two independent tasks through the normal host runner entry point.
"$python" -m host_runner -- bash "$script" --hold "$probe/first" "$probe/release-first" >"$probe/first.log" 2>&1 &
first=$!
await_task first "$first"
"$python" -m host_runner -- bash "$script" --hold "$probe/second" "$probe/release-second" >"$probe/second.log" 2>&1 &
second=$!
await_task second "$second"

# Assert: both leases see the same actual app/db and a dynamically published local port.
read -r url generation <"$probe/first"
read -r reused_url reused_generation <"$probe/second"
test "$reused_url $reused_generation" = "$url $generation"
test "$generation" != unmanaged
app_id=$(container_id app)
db_id=$(container_id db)
test "$(docker container inspect --format '{{index .Config.Labels "org.nir.runner.generation"}} {{.State.Health.Status}}' "$db_id")" = "$generation healthy"
test "$url" = "http://$(docker port "$app_id" 8000/tcp)"
[[ "$url" == http://127.0.0.1:* && "$url" != http://127.0.0.1:8000 ]]
grep -Fx "$url" "$probe/first.log"
grep -Fx "$url" "$probe/second.log"
assert_app "$app_id" "$url" "$generation"

# Act: normal first release, then TERM of the last owner.
touch "$probe/release-first"
wait "$first"
first=
# Assert: first release left the same app and database working.
test "$(container_id app)" = "$app_id"
test "$(container_id db)" = "$db_id"
curl -fsS -o /dev/null --max-time 3 "$url/admin/login/"

# Act
since=$(date +%s)
kill -TERM "$second"
status=0
wait "$second" || status=$?
second=
# Assert: the last release stopped the exact leased containers in app -> db order.
test "$status" = 143
test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
stops=$(docker events --since "$since" --until "$(( $(date +%s) + 1 ))" \
    --filter event=stop --filter "container=$app_id" --filter "container=$db_id" --format '{{.Actor.ID}}')
test "$stops" = "$(printf '%s\n%s' "$app_id" "$db_id")"
assert_volumes
curl -fsS --max-time 2 http://127.0.0.1:8000/marker | cmp -s "$probe/foreign/marker" -
printf 'Managed release order: app %s -> db %s (generation %s)\n' "$app_id" "$db_id" "$generation"

# Act: start again using the same persistent fixture volumes.
"$python" -m host_runner -- bash "$script" --hold "$probe/restart" "$probe/release-restart" >"$probe/restart.log" 2>&1 &
first=$!
await_task restart "$first"
# Assert: the generation and containers changed, and startup sees the retained data.
read -r restarted_url restarted_generation <"$probe/restart"
restarted_app=$(container_id app)
test "$restarted_app" != "$app_id"
test "$(container_id db)" != "$db_id"
test "$restarted_generation" != "$generation"
assert_app "$restarted_app" "$restarted_url" "$restarted_generation"
assert_volumes
touch "$probe/release-restart"
wait "$first"
first=
test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
assert_volumes
echo 'Managed runtime, two leases, TERM, occupied port and persistent restart passed.'
