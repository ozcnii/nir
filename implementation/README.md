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

### Первый запуск

Передайте `DB_APP_PASSWORD` и `DB_MIGRATOR_PASSWORD` через окружение
оператора. Если имя БД отличается от `catalog`, передайте там же `DB_NAME`.
Используйте выделенный Compose-проект под управлением host runner. Из папки
реализации:

```sh
uv run --locked python -m host_runner --db-only -- bash -ec '
    docker compose build app
    bash scripts/prepare-db.sh
    docker compose run --rm --no-deps app python manage.py createsuperuser
    docker compose run --rm --no-deps -T app python -m config.startup --check-only
'
```

Одноразовый процесс bootstrap проверяет подключение, существующие роли, владельцев
и схему до изменений. Он создаёт недостающие роли и выделенную БД. Затем скрипт
запускает штатный `migrate` под `migrator` и восстанавливает явную матрицу прав.
`createsuperuser` — отдельная команда оператора под ролью `app`. Введите пароль
в интерактивном запросе Django. При повторной подготовке используйте
`prepare-db.sh` и `config.startup --check-only`, чтобы сохранить созданного
администратора и данные.

После завершения DB-only аренды можно запустить app и базу обычным host runner.
При каждом запуске app проверяет применённые миграции и права роли `app`.
При ошибке проверка завершает процесс с причиной. После успешной проверки
стартует Django, и host runner публикует адрес после успешного healthcheck.
Обычный перезапуск не применяет миграции, не загружает справочники и не запускает
сбор. Для отдельной проверки первого запуска на новом проверочном томе из worktree
используйте `bash tests/startup/check-postgres.sh` с временными паролями.

### Восстановление стандартных записей

Начальная миграция заполняет справочники один раз. Восстановить выбранные
отсутствующие записи можно под ролью `migrator` из выделенного Compose-проекта.
Каждый параметр можно указать несколько раз.

```sh
uv run --locked python -m host_runner --db-only -- docker compose run --rm --no-deps -T \
    -e DB_MIGRATOR_PASSWORD -e DB_APP_PASSWORD= -e DJANGO_SECRET_KEY= app \
    python manage.py restore_reference_data --settings=config.settings_migrator \
    --source cian --property-type 1 --region 68
```

Команда сохраняет существующие значения и отменяет весь запрос, если подпись
восстанавливаемой записи уже занята другим ID. Конфликт можно устранить штатным
переименованием, затем повторить команду.

### Проверка прав PostgreSQL

Негативные SQL-проверки F25 запускаются только в отдельном worktree с новым
проверочным томом. Используйте отдельные временные пароли:

```sh
bash tests/db_roles/check-postgres.sh
```

Проверка резервирует новый том и запускает его через host runner. Существующий
том или контейнер этого worktree блокирует запуск до SQL-проверок.
