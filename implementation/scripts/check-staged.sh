#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
tool="$1"
shift
files=()
for path in "$@"; do
    [[ "$path" == implementation/* ]] || exit 2
    files+=("${path#implementation/}")
done

case "$tool" in
    ruff-lint) exec uv run --no-sync ruff check --no-fix "${files[@]}" ;;
    ruff-format) exec uv run --no-sync ruff format --check "${files[@]}" ;;
    djlint-lint) exec uv run --no-sync djlint --lint "${files[@]}" ;;
    djlint-format) exec uv run --no-sync djlint --check "${files[@]}" ;;
    *) exit 2 ;;
esac
