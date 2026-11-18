# Работа над реализацией

[Доска GitHub Projects](https://github.com/users/ozcnii/projects/1) — оперативные
статусы и представления «Доска»/«Все задачи». Проект приватный, доступен владельцу
и его агентам с авторизацией того же аккаунта; issues находятся в существующем публичном репозитории.

[Все рабочие задачи](https://github.com/ozcnii/nir/issues?q=is%3Aissue%20label%3Atask)
содержат границы, принятые спецификации, native зависимости, критерии и сценарии
проверки. Этот файл — только указатель; статусы/чекбоксы здесь не дублируются.

| Этап | Родительская карточка |
|---|---|
|1. Правила и каркас | [#1](https://github.com/ozcnii/nir/issues/1) |
|2. БД и первый запуск | [#2](https://github.com/ozcnii/nir/issues/2) |
|3. Жизненный цикл | [#3](https://github.com/ozcnii/nir/issues/3) |
|4. Первый сквозной проход | [#4](https://github.com/ozcnii/nir/issues/4) |
|5. Полный сбор и сопоставление | [#5](https://github.com/ozcnii/nir/issues/5) |
|6. Интерфейс и JSON | [#6](https://github.com/ozcnii/nir/issues/6) |
|7. Приёмка и исследование | [#7](https://github.com/ozcnii/nir/issues/7) |
|8. Итоговые материалы | [#8](https://github.com/ozcnii/nir/issues/8) |

Созданы 38 рабочих подзадач, без фиктивно завершённых работ. Каждая площадка имеет
свою задачу адаптера. T31 получает формальные основания заранее; T35 проверяет
нормоконтроль конечных страниц. Внешние подтверждения не подменяются отчётом агента.

Порядок: [CONTRIBUTING](../CONTRIBUTING.md), правила:
[AGENTS](../AGENTS.md), [CODING](../CODING.md). Принятый проект:
[сводка](CONSOLIDATED-DESIGN-9.md), [план реализации §10](PLAN.md).

Текущий режим работы указан в [WORKING-STATE.md](WORKING-STATE.md).
Исполнителей и статус приёмки назначает координатор через GitHub.

Источники готовых механизмов:
[Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/quickstart-for-projects),
[зависимости](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies),
[sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues),
[Issue Forms](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms).
Инструкции/статусы требуют соблюдения координатором и не являются автоматической
проверкой поведения приложения или атомарным механизмом распределения задач.
