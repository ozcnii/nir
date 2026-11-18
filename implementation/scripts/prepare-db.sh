#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
: "${COMPOSE_PROJECT_NAME:?Run under host_runner --db-only}"
: "${NIR_RUNNER_GENERATION:?Run under host_runner --db-only}"
if [[ "$NIR_RUNNER_GENERATION" == unmanaged ]]; then
    echo "Database preparation requires a managed host_runner lease." >&2
    exit 1
fi
: "${DB_APP_PASSWORD:?Set DB_APP_PASSWORD in the operator environment}"
: "${DB_MIGRATOR_PASSWORD:?Set DB_MIGRATOR_PASSWORD in the operator environment}"
export DB_NAME="${DB_NAME:-catalog}"

docker compose exec -T -e DB_NAME -e DB_APP_PASSWORD -e DB_MIGRATOR_PASSWORD db \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h db -U postgres -d postgres -v ON_ERROR_STOP=1' \
    < docker/bootstrap.sql

# Avoid passing runtime passwords or Django's secret key to the migrator.
docker compose run --rm --no-deps -T -e DB_MIGRATOR_PASSWORD \
    -e DB_APP_PASSWORD= -e DJANGO_SECRET_KEY= app \
    sh -ec 'python manage.py migrate --settings=config.settings_migrator && python -m config.db_roles apply'
