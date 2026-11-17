# 8.4 — первичные источники для локальных проверок, hooks и CI

Проверено 18 ноября 2026. Исследовательская записка к НЕ ПРИНЯТОМУ предложению 8.4. Не рабочая конфигурация и не разрешение установки. Принятые 8.1–8.3 не пересматриваются. Версии в документации ниже — версии источников и примеров, не выбранные версии приложения.

## Подтверждённые возможности и ограничения

### pre-commit

Документация поддерживает `repo: local`, выбор `stages`, фильтрацию файлов, `pass_filenames: false` (имена не добавляются к команде) и `require_serial: true` (один процесс). Последнее полезно для одной проверки проекта вместо независимых порций аргументов. `always_run` и передача имён — разные настройки. [Источник](https://pre-commit.com/#repository-local-hooks), [поля hook](https://pre-commit.com/#creating-new-hooks).

При commit проверяется staged-содержимое; unstaged-правки временно убираются. Это не обещание отсутствия любых конфликтов и не основание автоматически форматировать частично staged-файлы. [Источник](https://pre-commit.com/#pre-commit).

`install --hook-type pre-commit` явно выбирает только эту стадию. Migration mode сохраняет существующий hook выбранного типа; `--overwrite` его заменяет. [Источник](https://pre-commit.com/#pre-commit-install), [migration mode](https://pre-commit.com/#running-in-migration-mode).

Текущая документация называет системный тип `language: unsupported`: с 4.4.0 это новое имя `system`, старый alias обещан к удалению. Изолированное окружение для этого типа не создаётся: зависимости готовит пользователь. Окончательное имя зависит от ещё не выбранной версии pre-commit. [Источник](https://pre-commit.com/#unsupported).

Текущий проект 8.4: локальные hooks вызывают uv-команды Ruff/djLint только на проверку staged-файлов. Mypy не включён в commit hook: локально по §2 и в CI он проверяет весь собственный Python, включая migrations. Полные статические hooks при изменении общей конфигурации не принимают имена файлов (pass_filenames: false). Автоисправления, тесты и новый pre-push hook в commit-автоматизацию не включены. Существующий Git LFS pre-push сохранять; future install ограничить pre-commit. Добавление pre-commit в dev-зависимости отдельно НЕ ПРИНЯТО.

### uv и lock

`uv run` по умолчанию актуализирует lock и окружение. `--locked` запрещает изменение lock и выдаёт ошибку, если он не соответствует метаданным; `--frozen` использует lock без проверки его актуальности. `--no-sync` отключает проверку актуальности окружения. Следовательно, `--frozen` не заменяет проверку согласованности pyproject/lock, а `--no-sync` сам не подготавливает корректное окружение. [Источник](https://docs.astral.sh/uv/concepts/projects/sync/).

Текущий проект 8.4: после разрешения реализации подготовка — `uv sync --locked`; команды — `uv run --no-sync ...` только для подготовленного окружения. Перед app-проверками hook используется `uv sync --locked --check` с теми же группами: проверка синхронизации без установки; несоответствие даёт ошибку. Свежесть lock отдельно проверяет `uv lock --check`. Режим no-sync сам не проверяет эти условия. Флаги `sync --check` и `lock --check` описаны в [uv CLI](https://docs.astral.sh/uv/reference/cli/). Совместимость закрепляется выбранной версией; сейчас ни lock, ни окружение не создавались.

### GitHub Actions: условия запуска и required checks

Пропуск workflow через `paths`/`paths-ignore`, branch filter или commit message оставляет связанные checks в Pending; required check тогда блокирует merge. `permissions` можно ограничить на workflow/job; при явном перечислении прочие права получают `none`. [Источник](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

Проектное предложение: workflow для PR запускается без path-фильтра верхнего уровня, сначала определяет затронутую область, затем включает нужные jobs. Для docs-only изменений PostgreSQL и браузер не запускаются. Required gate имеет стабильное имя и проверяет итог нужных jobs; сбой определения области или нужного job должен давать ошибку. Реальное поведение gate при success/failure/skipped/cancelled требуется проверить после реализации; один текст не доказывает защиты ветки. Настройка required checks в GitHub — отдельное действие после появления проверенного CI, не выполненное исследованием.

### GitHub Actions: PR, forks, права и зависимости

`pull_request` работает с merge-контекстом PR. В fork PR обычно недоступны secrets, кроме GITHUB_TOKEN с read-only правами; запуск может потребовать одобрения, а private forks зависят от настроек. `pull_request_target` работает в контексте базового репозитория; GitHub рекомендует избегать его для сборки/запуска PR-кода из-за доступа к правам и secrets. [Источник](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).

Проектное предложение: `pull_request`, минимальное `contents: read`, без production secrets, публикации, deployments и запуска кода PR через `pull_request_target`. Тесты используют контролируемые локальные данные, без реальных обращений к площадкам недвижимости. Возможность fork-запуска и внешние ограничения аккаунта пока не проверены.

Полный commit SHA фиксирует версию action; следует проверять его происхождение из исходного action-репозитория. Tags могут изменяться. Актуальная security-документация также описывает workflow dependency lockfiles, включая транзитивные actions, как более сильный механизм для репозиториев, которые его приняли; нельзя называть SHA абсолютной защитой всей цепочки. [Источник](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions).

Проектное предложение: явно выбрать и проверить SHA используемых actions перед реализацией; не копировать изменяемые теги примеров и не добавлять новый механизм lockfiles без отдельного решения. Конкретные SHA сейчас не выбраны.

### PostgreSQL service

GitHub показывает service containers PostgreSQL с `pg_isready` healthcheck. Для services нужен Linux runner; при работе job непосредственно на runner БД доступна через localhost и опубликованный порт, при container job — по имени сервиса в общей сети. [Источник](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers).

Проектное предложение: одна Ubuntu/Python/PostgreSQL комбинация, соответствующая будущей поддерживаемой среде, отдельная тестовая БД и тестовые credentials без постоянных volumes. Версии Python/PostgreSQL/runner пока не приняты; `latest` в учебном примере не является нашим выбором. PostgreSQL нужен только jobs с реальной БД, а не lint/typecheck/docs. Healthcheck не заменяет миграции и интеграционные проверки.

### Django unittest и Playwright Python без pytest

Django использует стандартный unittest; `manage.py test` поддерживает выбор package/module/class/method. DB-тесты требуют подходящих Django TestCase, а не произвольного unittest.TestCase. [Источник](https://docs.djangoproject.com/en/5.2/topics/testing/overview/).

LiveServerTestCase запускает локальный сервер на свободном порту и завершает его после тестов; StaticLiveServerTestCase обслуживает static files. Пример Django использует Selenium, поэтому он не является готовой реализацией интеграции Playwright. [Источник](https://docs.djangoproject.com/en/5.2/topics/testing/tools/#liveservertestcase).

Playwright Python доступен как библиотека через sync/async API и запускает браузер из обычного Python-скрипта; pytest не обязателен для библиотечного API. [Источник](https://playwright.dev/python/docs/library). CI требует браузерных бинарных файлов и системных зависимостей; документация показывает `python -m playwright install --with-deps`, но её пример запуска через pytest относится к предлагаемому документацией тестовому стеку. Он не вводит pytest в наш проект. [Источник](https://playwright.dev/python/docs/ci).

Проектное предложение: сохранить нативный runner Django/unittest, библиотеку Playwright Python и локальный live server; при выборе только Chromium устанавливать только нужный browser. Контексты браузера, traces/screenshots при сбое и cleanup оформляются кодом будущих тестов, а не pytest fixtures/флагами. Unit-тесты без БД не должны требовать PostgreSQL/браузер. Полный релевантный CI-набор допускается на PR с кодом; полный набор на каждый commit не предлагается.

## Граница результата

Проверены официальные описания механизмов. Это не запуск локальных hooks, CI, сервиса БД или тестов будущего приложения. Совместимые версии, реальные команды/пути проекта, полнота фильтра затронутых файлов, partial-stage сценарий, сохранность LFS hook, обязательность checks и fork-поведение ещё требуют проверки при отдельно разрешённой реализации. Исследование не меняет принятые Ruff 88, strict mypy для всего собственного Python и djLint Django 2/120.
