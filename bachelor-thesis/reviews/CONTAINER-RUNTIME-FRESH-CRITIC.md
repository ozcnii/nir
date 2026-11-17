# Независимый критик: порты, пользователь app и Chromium

18 ноября 2026. Новый агент fresh_container_runtime_critic, fork_turns=none. Read-only current-state review проекта, без чтения старых отзывов/оценок, DECISIONS/WORKING-STATE и постороннего navyki.txt. Использован code-review skill.

## Итог

Первичный отзыв:8,5/10. Найдено одно P2 Spec: основной документ выбирал готовый официальный seccomp artifact, evidence допускала самостоятельное построение на актуальном Docker default. Это неоднозначный воспроизводимый результат, не доказанная ошибка самого sandbox.

Правка: оба текста выбирают один готовый полный официальный профиль Playwright; собственный синтез исключён, commit/hash фиксируются при реализации, Engine-совместимость проверяется отдельно. Тот же критик точечно перечитал исправленные строки26/31 и подтвердил устранениеP2. Это точечный follow-up, не второй новый независимый обзор.

**Обновлённая оценка:9/10. Открытых замечаний Standards/Spec в проверенном объёме нет; достаточно для обсуждения и принятия владельцем.** Новые решения пока НЕ ПРИНЯТЫ.

## Что реально проверено

- Loopback host-публикация отдельно от внутреннего bind0.0.0.0:8000, db:5432 без host-порта по Docker ports/Compose networking.
- Linux appUID не смешан с PostgreSQLapp/migrator; схема одноразовой миграции и credentials не пересмотрена.
- Полный bundledChromium через channelchromium, установка безshell, общий browser path и APIchromium_sandbox defaultfalse; сверена документация и PythonAPIv1.65.2.
- Nonroot/seccomp/namespaces не объявлены достаточным фактическим доказательством работающего sandbox; verified upstreamdefault--disable-dev-shm-usage отличается от ещё не проверенного pinneddriver.
- init не подменяет finally/полное владение/shutdown; согласовано с4.3 и8.3; ненужного постоянного сервиса/настроек памяти нет.

Проверенные ограничения: CONTAINER-PROFILE, FIRST-LAUNCH-ROLES, INITIAL-DATA, COLLECTION-4.3, QUALITY-8.3/8.5. [Первичные ссылки](../evidence/CONTAINER-RUNTIME-PRIMARY-SOURCES.md).

## Снимок после исправления

- CONTAINER-RUNTIME-PROPOSAL.md SHA256: efc81a1656d77c476e01df728d3bb34ebb76635ef4f33b57ccc8272403535365
- evidence/CONTAINER-RUNTIME-PRIMARY-SOURCES.md SHA256: 4e41077d5a6b496169ac49b10b69ef63cb17f3bc1fe414697ceddde540e6ae8d

Ограничения: не запускались сборка, контейнеры, SQL, Chromium; не проверены фактические UID/права файлов, sandbox/ядроDockerVM, pinneddriverargs, RAM/tmp и остановка дерева процессов/HTTPсценарий. Никаких изменений кода/config/установок агент не выполнял. Оценка относится к проекту, не к работающему окружению.
