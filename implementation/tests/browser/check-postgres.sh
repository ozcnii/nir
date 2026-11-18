#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source tests/disposable-postgres.sh

if [[ ${1:-} != --leased ]]; then
    run_disposable_postgres_with_cleanup t10_browser_test bash "$0" --leased
    exit 0
fi

# Arrange
assert_disposable_postgres
docker compose build app
bash scripts/prepare-db.sh
admin -d "$DB_NAME" -c '
    CREATE TABLE public.t10_probe (id integer PRIMARY KEY);
    GRANT INSERT, SELECT ON public.t10_probe TO app;
'
fixture=(--no-deps -T -v "$PWD/tests/browser:/test-suite:ro" -e PYTHONPATH=/app)

# Act
suite_result=0
docker compose run --rm "${fixture[@]}" app     python /test-suite/check_browser_lifecycle.py || suite_result=$?

# Assert
test "$suite_result" = 0

# Arrange the container-level TERM case with the launcher directly under init.
app=$(docker compose run -d "${fixture[@]}" app     python /test-suite/check_browser_lifecycle.py app-launcher /run/app/app-term)
cleanup_app() {
    local generation
    generation=$(docker container inspect --format '{{index .Config.Labels "org.nir.runner.generation"}}' "$app")
    if [[ "$generation" == "$NIR_RUNNER_GENERATION" ]]; then
        if [[ "$(docker container inspect --format '{{.State.Running}}' "$app")" == true ]]; then
            docker container stop --time 60 "$app" >/dev/null
        fi
        docker container rm "$app" >/dev/null
    fi
}
trap cleanup_app EXIT
docker exec "$app" python -c '
import time
from pathlib import Path
from runtime.control import CONTROL_PATH, request

deadline = time.monotonic() + 15
fixture = Path("/run/app/app-term")
while not CONTROL_PATH.exists() or not (fixture / "port").exists():
    if time.monotonic() >= deadline:
        raise SystemExit("Launcher socket/web did not start")
    time.sleep(.03)
selection = {"sources":["cian"], "regions":[68], "categories":["resale"], "deal_types":["sale"]}
assert request({"action":"start", "selection": selection})["ok"] is True
while request({"action":"status"})["state"] != "running":
    if time.monotonic() >= deadline:
        raise SystemExit("Browser runner did not become ready")
    time.sleep(.03)
assert (fixture / "port").exists()
'

# Act
docker container stop --time 60 "$app" >/dev/null

# Assert
test "$(docker container inspect --format '{{.State.ExitCode}}' "$app")" = 0
docker logs "$app" 2>&1 | grep -F 'T10: browser runner completed cooperative cleanup.'
test "$(admin -d "$DB_NAME" -At -c "SELECT count(*) FROM public.t10_probe WHERE id=20")" = 1
admin -d "$DB_NAME" -At -c 'SELECT 1' | grep -Fx 1
