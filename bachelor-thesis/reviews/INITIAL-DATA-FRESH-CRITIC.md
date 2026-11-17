# Начальные данные — новый чистый критик

18 ноября 2026. Агент fresh_initial_data_critic без истории переписки, прежних отзывов и оценок. Главный агент сохранил полученный результат. Scope: INITIAL-DATA-PROPOSAL.md, evidence/INITIAL-DATA-PRIMARY-SOURCES.md и новый непринятый §4 FIRST-LAUNCH-ROLES-PROPOSAL.md; ограничения — действующий DB-POINT2-VERIFIED-DRAFT.md,5.1/5.4/5.5, принятые роли/запуск и граница queries→collection.

**Итог: 9/10. Для обсуждения/принятия политики достаточно. Подтверждённых замечаний Standards/Spec не найдено. Предложение НЕ ПРИНЯТО владельцем.** Оценка проекта политики, не её работающей реализации.

## Standards

Схема sources(code,enabled), regions/property_types(id,name), разрешённый CRUD и передача простых значений через queries сохранены. Нет новых mapping-таблиц/профилей/скрытой оркестрации. Management command обоснована как явное служебное действие на стандартном механизме Django; runtime-права не расширяются.

## Spec

Известный ID определяет идентичность, name редактируется как подпись. Удаление unused стандартной строки допустимо; restart её не возвращает. Новая одноимённая строка с другим ID не получает старую поддержку. Явное восстановление создаёт отсутствующий стандартный PK, сохраняет существующие подписи/флаги/FK; конфликт имени откатывает весь выбранный набор. Применённая migration и закрытое редактирование PK задают происхождение штатного пути; произвольный SQL-takeover нельзя распознать по id/name, и обратное не обещается.

Вручную сверены перечень Минюста и группы. Локально подтверждены89уникальныхстрок, непрерывные ID1–89, точное совпадение приложения со source-snapshot, Самара68/Москва82. Это выбранный список и наши PK, не native IDs площадок. [Первичный перечень](https://minjust.gov.ru/ru/documents/8011/).

Data migrations/fixtures, historical models, get_or_create, ручные PK и atomic согласуются с первичными [initial data](https://docs.djangoproject.com/en/5.2/howto/initial-data/), [historical models](https://docs.djangoproject.com/en/5.2/topics/migrations/#historical-models), [get_or_create](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#get-or-create), [PK sequences](https://docs.djangoproject.com/en/5.2/ref/databases/#manually-specifying-values-of-auto-incrementing-primary-keys), [transactions](https://docs.djangoproject.com/en/5.2/topics/db/transactions/). Атомарность всего выбранного восстановления требует внешней транзакции, это явно предусмотрено.

Неблокирующее уточнение для реализации: fixtures должны использовать выбранные реальные seed-ID (Самара68); прежние P2/JSON примеры Самара1 прямо условные и не противоречат формату. Главный агент добавил в предложение соответствующее пояснение без изменения данных/политики.

## Пределы

Проверен текущий документ, не базовый diff. SQL/приложение/контейнеры/миграции/гонки/grants/маршруты всех регионов не запускались. Последовательности/права после выполнения предстоит подтвердить будущими проверками. Файлы не менялись, прежние reviews не читались.

Исходный snapshot до добавления итоговой отметки: proposal SHA256 e31514f6ce6ac4ebd1e8b7e8b3e1249912c73d6d5360059ee7790b45f4e0c7de; evidence c51eb182128c15ea3d2bcc679ee88c08ea3dd68f5d6caa8ee626962ad4f9b92d.
