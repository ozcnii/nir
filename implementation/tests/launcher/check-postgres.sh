#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh

if [[ ${1:-} != --leased ]]; then
    run_disposable_postgres_with_cleanup t09_launcher_test bash "$0" --leased
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
# Act
suite_result=0
docker compose -f compose.yaml run --rm --no-deps -T \
    -v "$PWD/tests/launcher:/test-suite:ro" \
    -e PYTHONPATH=/app app python /test-suite/check_launcher_postgres.py || suite_result=$?

# Assert
test "$suite_result" = 0

# Act: the journal fixture uses the same disposable database and control boundary.
journal_result=0
docker compose -f compose.yaml run --rm --no-deps -T \
    -v "$PWD/tests/journal:/test-suite:ro" \
    -e PYTHONPATH=/app app python /test-suite/check_journal.py || journal_result=$?

# Assert
test "$journal_result" = 0
