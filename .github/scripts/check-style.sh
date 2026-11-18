#!/usr/bin/env bash
set -euo pipefail

# Both CI and the profile-change hook use the same tracked scope.
paths=$(mktemp)
trap 'rm -f "$paths"' EXIT
git ls-files -z -- manage.py config catalog collection > "$paths"

python_files=()
templates=()
while IFS= read -r -d '' path; do
    [[ -f "$path" ]] || continue
    case "$path" in
        manage.py|config/*.py|catalog/*.py|collection/*.py) python_files+=("$path") ;;
        catalog/templates/*.html|collection/templates/*.html) templates+=("$path") ;;
    esac
done < "$paths"

if (( ${#python_files[@]} )); then
    uv run --no-sync ruff check --no-fix "${python_files[@]}"
    uv run --no-sync ruff format --check "${python_files[@]}"
    if [[ "${1:-}" == --types ]]; then
        uv run --no-sync mypy "${python_files[@]}"
    fi
else
    echo "Python application files are absent; Python checks do not apply."
fi
if (( ${#templates[@]} )); then
    uv run --no-sync djlint --lint "${templates[@]}"
    uv run --no-sync djlint --check "${templates[@]}"
else
    echo "Django template checks do not apply."
fi
