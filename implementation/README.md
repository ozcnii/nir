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

Для классических областных выдач Циан есть отдельный режим той же команды.
Пример продажи квартир любого сегмента в Самарской области:

```sh
docker compose run --rm app python -m app.manage collect \
  --source cian --region 68 --property-type apartment --deal sale
```

Команда принимает ID субъекта из справочника. Для Самарской области подключены
обычные маршруты продажи и месячной аренды квартир, комнат и домов.
Для Саратовской области добавлен просмотренный маршрут продажи квартир.
Если область или сочетание отсутствует в таблице подтверждённых маршрутов,
команда возвращает `unsupported_collection_scope` до открытия браузера.
Многоплощадочный обход, суточные выдачи и раскрытие групп пока не реализованы.
При неподтверждённом конце, потере фильтра или нераскрытой группе вывод содержит
`listing_complete: false` и причину. Допроверка отсутствующих объявлений в этом случае не начинается.

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
