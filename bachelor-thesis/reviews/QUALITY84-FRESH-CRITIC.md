# 8.4 — новый чистый критик локальных проверок, hooks и CI

18 ноября 2026. Новый независимый агент fresh_quality84_critic, без истории переписки, прежних отзывов и оценок. Главный агент сохранил ответы рецензента. Scope: QUALITY-8.4-PROPOSAL.md и evidence/QUALITY84-PRIMARY-SOURCES.md; контекст — CLAUDE.md, skill code-review, принятые 8.1–8.3, 7.1 и статусная таблица PLAN.

**Итог: 9/10, достаточно для согласования предложения 8.4. Standards — открытых замечаний нет; Spec — открытых замечаний нет.** Оценка проекта автоматизации, не работающих hooks/CI; 8.4 владельцем пока НЕ ПРИНЯТ.

## Standards

Существенных замечаний нет. Найдено небольшое замечание P3: устаревшая ссылка required checks в proposal возвращала 404. Главный агент заменил её на [актуальную официальную страницу GitHub](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks). Рецензент адресно проверил исправление: старый URL отсутствует в обоих scoped файлах, P3 закрыт. Полного повторного обзора не было.

## Spec

Предложение сохраняет принятые Ruff/mypy/djLint и охват собственного Python, включая migrations; исключает mypy/БД/браузер из быстрого commit hook; сохраняет LFS pre-push; явно предлагает новую dev-зависимость и CI без заявления об их принятии. Три jobs дают реализуемую схему docs-only без PostgreSQL и обязательного итогового результата. Неизвестные пути, удаления/rename, ошибки классификации и зависимости skipped/failed/cancelled учтены как требования будущей реализации.

## Что проверено

Текущие тексты обоих scoped файлов. В процессе источниковая записка содержала другие варианты mypy-in-hook и uv run --locked; ведущий синхронизировал два абзаца с текущим предложением, рецензент повторно их прочитал и подтвердил устранение расхождений. Локально подтверждены отсутствие pre-commit hook и сохранённый LFS pre-push, назначение npm Playwright для документных снимков.

Первичные документы: [uv CLI](https://docs.astral.sh/uv/reference/cli/), [pre-commit](https://pre-commit.com/#unsupported), [Playwright library](https://playwright.dev/python/docs/library), [Django LiveServer](https://docs.djangoproject.com/en/5.2/topics/testing/tools/#liveservertestcase), [PostgreSQL services](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers), [GitHub security](https://docs.github.com/en/actions/reference/security/secure-use). Поведение Pending/skipped и always для зависимого итогового job сверено с официальной страницей required checks.

## Пределы

Read-only current-state review документов, не проверка исполняемой конфигурации. Установок, сервисов, тестов, Git mutations и дальнейшей делегации у рецензента не было. Реальные фильтры hooks, partial staging, совместимость выбранных версий, gate cancellation, права F25 и browser lifecycle остаются будущими испытаниями; текст это обозначает. Исправление URL перепроверено отдельно без нового полного круга. Код и рабочие конфигурации не создавались.
