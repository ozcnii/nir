# 8.5 — первичные доказательства версий и совместимости

Проверено 18 ноября 2026. Главный агент прочитал официальные страницы, PyPI JSON metadata, GitHub release/tag API и Docker Hub metadata. Это исследование публикаций, требований и доступных артефактов, не установка/запуск и не resolver. Первоначальный исследовательский агент успел сообщить основные версии и матрицу, затем завершился по лимиту; файл и доказательства собраны главным агентом, факты перепроверены напрямую. Старые оценки не используются.

## 1. Основная связка

| Проверенный факт | Первичный источник |
|---|---|
| Python 3.13.18 выпущен 25 октября 2026, security release; Python.org предоставляет исходники без новых binary installers. | [Страница 3.13.18](https://www.python.org/downloads/release/python-31318/) |
| Django 5.2.19 — security patch от 3 ноября 2026 в LTS-ветке 5.2; extended support 5.2 до апреля 2028. | [Release notes](https://docs.djangoproject.com/en/5.2/releases/5.2.19/), [таблица поддержки](https://www.djangoproject.com/download/) |
| Django 5.2 поддерживает PostgreSQL 14+, psycopg 3.1.8+; 3.x рекомендуемая линия. | [Django databases](https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes) |
| PostgreSQL 17.12 опубликован 12 ноября 2026; это minor в поддерживаемой ветке 17, а не новый major. | [17.12 release notes](https://www.postgresql.org/docs/release/17.12/), [support/versioning](https://www.postgresql.org/support/versioning/) |
| Tag django-stubs 5.2.9: полная Django 5.2, Python 3.10–3.13, mypy 1.13–1.19. Совпадение patch-номеров Django/stubs не требуется. | [README закреплённого tag](https://github.com/typeddjango/django-stubs/blob/5.2.9/README.md), [raw README](https://raw.githubusercontent.com/typeddjango/django-stubs/5.2.9/README.md) |
| Metadata stubs 5.2.9: Python>=3.10, django-stubs-ext>=5.2.9, compatible-mypy требует >=1.13,<1.20. Ext 5.2.9 опубликован, Python>=3.10. | [stubs JSON](https://pypi.org/pypi/django-stubs/5.2.9/json), [ext JSON](https://pypi.org/pypi/django-stubs-ext/5.2.9/json) |
| Mypy 1.19.1 опубликован и не yanked; Python>=3.9. Текущий PyPI mypy 2.4.1 не попадает в указанный диапазон выбранного stubs. | [1.19.1 JSON](https://pypi.org/pypi/mypy/1.19.1/json), [актуальный mypy JSON](https://pypi.org/pypi/mypy/json) |

## 2. Metadata предлагаемых пакетов

Прочитаны info.version/requires_python/requires_dist и urls, где применимо releases; версии основных выбранных пакетов существуют и не помечены yanked. «Latest» ниже — наблюдение на дату исследования, не стратегия плавающей зависимости. Requires-Python — ограничение установки, не доказательство полной корректности на любой версии Python.

| Пакет | Предлагаемая версия | Requires-Python | Источник |
|---|---|---|---|
| Django | 5.2.19 | >=3.10 | [JSON версии](https://pypi.org/pypi/Django/5.2.19/json) |
| psycopg / psycopg-binary | 3.3.9 / 3.3.9 | >=3.10 | [psycopg JSON](https://pypi.org/pypi/psycopg/3.3.9/json), [binary JSON](https://pypi.org/pypi/psycopg-binary/3.3.9/json) |
| uv | 0.13.9 | >=3.8 для Python-distribution | [JSON](https://pypi.org/pypi/uv/0.13.9/json), [release](https://github.com/astral-sh/uv/releases/tag/0.13.9) |
| Ruff | 0.17.5 | >=3.7 для Python-distribution | [JSON](https://pypi.org/pypi/ruff/0.17.5/json) |
| djLint | 1.53.1 | >=3.11 | [JSON](https://pypi.org/pypi/djlint/1.53.1/json) |
| pre-commit | 4.7.0 | >=3.11 | [JSON](https://pypi.org/pypi/pre-commit/4.7.0/json), [официальные local hooks](https://pre-commit.com/#repository-local-hooks) |
| Playwright | 1.65.2 | >=3.10 | [JSON](https://pypi.org/pypi/playwright/1.65.2/json), [library](https://playwright.dev/python/docs/library) |

В закреплённых исходниках дополнительно найдены определения CLI sync/check/locked/no-sync у [uv 0.13.9](https://raw.githubusercontent.com/astral-sh/uv/0.13.9/crates/uv-cli/src/lib.rs), backend unsupported в [pre-commit v4.7.0](https://raw.githubusercontent.com/pre-commit/pre-commit/v4.7.0/pre_commit/all_languages.py), strict/warn-unreachable/disallow-any-unimported в [mypy v1.19.1](https://raw.githubusercontent.com/python/mypy/v1.19.1/mypy/main.py). Это чтение исходников, не запуск CLI и не полная проверка будущего config.

Psycopg extra binary тянет psycopg-binary той же версии 3.3.9. Его extras test/dev включают собственные тестовые инструменты автора; они не становятся зависимостями нашего приложения. Playwright Python — отдельный пакет от существующего npm Playwright для снимков документов; npm-версия не обновляется автоматически.

## 3. Проверенные платформенные артефакты

PyPI urls выбранных версий содержат, среди прочего:

- psycopg_binary-3.3.9-cp313-cp313-macosx_11_0_arm64.whl и Linux manylinux2014_x86_64/manylinux_2_17;
- mypy-1.19.1-cp313-cp313-macosx_11_0_arm64.whl и Linux manylinux x86_64;
- playwright-1.65.2-py3-none-macosx_11_0_arm64.whl и py3-none-manylinux1_x86_64.whl.

Это наличие готовых distributions, не проверка browser runtime. Текущая [Playwright system requirements](https://playwright.dev/python/docs/intro#system-requirements) перечисляет macOS14+, Ubuntu24.04 x86-64/ARM64. Общая страница содержит более широкий минимум Python; для конкретного пакета 1.65.2 применяем Requires-Python>=3.10 из metadata. Локальная read-only проверка sw_vers/uname показала macOS27.0.1 ARM64; приложение/браузер не запускались.

В [manifest uv именно 0.13.9](https://raw.githubusercontent.com/astral-sh/uv/0.13.9/crates/uv-python-managed/download-metadata.json) найдены CPython 3.13.18 обычной сборки, build 20261110:

- key cpython-3.13.18-darwin-aarch64-none → [Astral Darwin ARM64 archive](https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.13.18%2B20261110-aarch64-apple-darwin-install_only_stripped.tar.gz), SHA-256 7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540;
- key cpython-3.13.18-linux-x86_64-gnu — обычная GNU/Linux x86-64 сборка того же релиза.

Архивы не скачивались. Первоначальный старый путь crates/uv-python/download-metadata.json вернул404; использован актуальный crates/uv-python-managed. Доступность asset не заменяет проверку установки и системных библиотек. Браузерная ревизия берётся установщиком выбранного Playwright, не отождествляется с версией npm/системного Chrome. Прямой guessed URL v1.65.2 в другом upstream не подтвердился, поэтому числовая ревизия Chromium здесь не выдумана.

## 4. Артефакты CI

GitHub REST releases/latest на дату проверки и commits/<tag> разрешили теги в commits:

| Repository | Tag | Commit |
|---|---|---|
| actions/checkout | v7.0.1 | 3d3c42e5aac5ba805825da76410c181273ba90b1 |
| astral-sh/setup-uv | v10.3.3 | 68559e99cc807c1eb7e9238fd43a15bba460eec2 |
| actions/upload-artifact | v7.0.2 | cf430e030ddbb5b0abf93d22962f4752f3646cd9 |

Источники: [checkout release](https://github.com/actions/checkout/releases/tag/v7.0.1), [setup-uv release](https://github.com/astral-sh/setup-uv/releases/tag/v10.3.3), [upload-artifact release](https://github.com/actions/upload-artifact/releases/tag/v7.0.2). Для каждого вызваны api.github.com/repos/<repository>/releases/latest и api.github.com/repos/<repository>/commits/<tag>. Сам workflow/actions не запускались.

[Docker Hub metadata официального postgres:17.12-bookworm](https://hub.docker.com/v2/repositories/library/postgres/tags/17.12-bookworm) сообщает digest sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536 и платформы Linux amd64/arm64. Образ не загружался. Доступность tag/digest не является проверкой миграций/прикладной роли. ubuntu-24.04 — предлагаемый runner, обслуживаемый GitHub и не immutable snapshot всех OS packages.

## 5. Что из этого следует и что остаётся непроверенным

Документированно согласованный кандидат: Python3.13.18 + Django5.2.19 + PostgreSQL17.12 + psycopg[binary]3.3.9; stubs/ext5.2.9 + mypy1.19.1; остальные инструменты из таблицы. Это предложение8.5, не уже принятое решение.

Не выполнялись создание lock/резолв всех транзитивных зависимостей, установка/импорт инструментов, mypy-plugin, проверка флагов каждого выбранного бинарника, миграции, PostgreSQL/браузерные тесты или workflow. Перед применением нужны разрешённая реализация и проход 8.2/8.4; при конфликте — конкретное обоснованное изменение владельцу, без молчаливой подмены версий/ослабления strict. Фиксация известных версий не означает бессрочный отказ от обновлений безопасности.
