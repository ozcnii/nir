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

Для задач с локальным стеком используется host runner. Из папки реализации:

```sh
uv run --locked python -m host_runner -- <команда задачи>
uv run --locked python -m host_runner --db-only -- <команда задачи>
```

Первый режим арендует app и PostgreSQL, второй только PostgreSQL. Команда
остаётся на переднем плане до окончания использования стека.

## Контейнеры

Из этой папки `docker compose build app` собирает локальный образ без запуска
сервисов. Для db используется штатный образ PostgreSQL. Docker Desktop 4.94.0
с Engine 29.8.2 и Compose 5.5.1 подходит для локальной проверки.

Для старта PostgreSQL оператор передаёт `POSTGRES_PASSWORD`. Для приложения
используются `DB_NAME` (по умолчанию `catalog`), `DB_APP_PASSWORD` и
`DJANGO_SECRET_KEY`. Секреты передаются из локального окружения или
игнорируемого файла `.env` и отсутствуют в образе.

### Подготовка выделенной БД

Передайте `DB_APP_PASSWORD` и `DB_MIGRATOR_PASSWORD` через окружение
оператора. Если имя БД отличается от `catalog`, передайте там же `DB_NAME`. Используйте выделенный Compose-проект, которым управляет
host runner. Из папки реализации:

```sh
uv run --locked python -m host_runner --db-only -- bash -ec '
    docker compose build app
    bash scripts/prepare-db.sh
    docker compose run --rm --no-deps -T app python -m config.db_roles check
'
```

Скрипт проверяет подключение к служебной БД, создаёт отсутствующие роли и
прикладную БД, запускает штатный `migrate` под `migrator` и восстанавливает
явную матрицу прав. Повтор сохраняет данные и пароли существующих ролей.
Конфликт владельца останавливает подготовку. `config.db_roles check` читает
метаданные под ролью `app`. Первый запуск администратора сайта и preflight
перед web входят в T08.

Негативные SQL-проверки F25 запускаются только в отдельном worktree с новым
проверочным томом. Используйте отдельные временные пароли:

```sh
uv run --locked python -m host_runner --db-only -- bash tests/db_roles/check-postgres.sh
```
