# Реализация ВКР

Приложение и сбор находятся в [app/](app/). Настройки Django лежат в
[app/config/](app/config/), Dockerfile и профиль Chromium в [docker/](docker/),
команды разработчика в [tools/](tools/), сквозные проверки в [tests/](tests/).
Учебные документы находятся в [thesis/](../thesis/README.md).

## Первый запуск

Из каталога `implementation/` задайте в окружении `POSTGRES_PASSWORD`,
`DB_MIGRATOR_PASSWORD`, `DB_APP_PASSWORD` и `DJANGO_SECRET_KEY`. Используйте
свои значения. `DB_NAME` по умолчанию равен `catalog`.

```sh
docker compose build app
docker compose up -d --wait db
bash tools/prepare-db.sh
docker compose up -d --wait app
```

Подготовка создаёт необходимые роли и БД, применяет миграции и выдаёт права
приложению. Подготовку одной БД выполняет один оператор за раз. Перед запуском
сайта Django проверяет миграции штатной командой `migrate --check`.
При последующих запусках используйте `docker compose up -d --wait`.
После обновления с новыми миграциями сначала выполните `tools/prepare-db.sh`.

Для подключения существующего тома PostgreSQL задайте в
`COMPOSE_PROJECT_NAME` имя из его метки `com.docker.compose.project`.
Compose использует этот том с сохранёнными объявлениями.

Сайт открыт по адресу `http://127.0.0.1:8000`. Если порт занят, задайте
свободный `APP_PORT`. Для создания администратора выполните:

```sh
docker compose exec app python -m app.manage createsuperuser
```

## Сбор

Сбор одной карточки запускается отдельной командой Django. Поддержаны
проверенные маршруты Циан, [Авито](app/collection/collectors/avito.md) и Домклик,
включая партнёрские карточки Циан и Домклик и витрину Домклик 24.
Пример запуска для Циан:

```sh
docker compose run --rm app python -m app.manage collect 'https://samara.cian.ru/sale/flat/330631159/'
```

Остановка в переднем плане — `Ctrl+C`. Команда выводит в терминал итог или
ошибку. Если нужен вывод после остановки, запускайте без `--rm` и читайте
его через `docker logs ИМЯ_КОНТЕЙНЕРА`. Для веб-сервиса и БД используйте
`docker compose logs -f`.
Для остановки своего стека используйте `docker compose down`. Данные
PostgreSQL остаются в томе `db`.

## Проверки кода

```sh
uv sync --locked --dev
bash tools/check-style.sh --types
uv run --no-sync python -m app.manage check --settings=app.config.settings_base
```

Правила и границы модулей описаны в [CODING.md](../CODING.md).
Сквозная проверка в отдельном проекте Compose запускается командой
`bash tests/compose.sh`.
