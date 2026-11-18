# Реализация ВКР

Здесь находятся приложение Django, его окружение, тесты и инструменты запуска.
Принятый проект описан в [сводке](../bachelor-thesis/CONSOLIDATED-DESIGN-9.md).
Пояснительная записка находится в [thesis](../thesis/README.md).

Из этой папки, через uv 0.13.9:

```sh
uv sync --locked --dev
uv run --no-sync python manage.py check --settings=config.settings_base
bash scripts/check-style.sh --types
```

Для установки Git hook из корня репозитория:

```sh
uv --project implementation run --no-sync pre-commit install --hook-type pre-commit
```
