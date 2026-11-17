# Новый независимый критик: остановка процессов и освобождение ресурсов

18 ноября 2026. Агент fresh_process_lifecycle_critic, fork_turns=none. Read-only current-state review по code-review skill; старые reviews/DECISIONS/WORKING-STATE не открывал, постороннийnavyki.txt не читал. Сверены текущая схема, первичныеисточники и принятые4.3/5.4/runtime; не ревью реализации.

## Итог

**9/10. Для согласования достаточно. Открытых подтверждённых замечанийP1–P3 нет.**

Standards: launcher/локальныйsocket имеют конкретную роль одного родителя процессов независимо от webbackend. Новых сервисов, очередей, доменных таблиц или выбора HTTPсервера нет. Все новые механизмы названы предложением, не работающей автоуборкой.

Spec: HTTPstop отделён от завершения стека, старыйrun_id не влияет на новый. Неопределённая очистка блокируетnewrun/delete; PGsessionlock не доказывает завершениеChromium. Hostаренды защищаютapp/db каждого task, неопределённый/живойвладелец не удаляется по одномуheartbeat, acquire/release/stopping сериализованы. Volume и чужие сервисы сохраняются.

Самостоятельно сверены upstreamPlaywright processLauncher.ts/driver.ts (detached browser group/EOFcleanup), Pythonpidfd, PGsessionlock, Composeports. Уточнение сигнала черезpidfd исключает адресацию повторноиспользованногоPID; групповойSIGKILL не предлагается. Порядок8000→подтверждённыйbindconflict→Dockerallocated соответствует уточнению владельца, не противоречит факту способностиDockerраспределятьпорт.

После основногоотзыва тот же критик точечно перечитал подразделсинхронизацииstart/delete: удаление держитxactlockдоcommit и проверяетlauncherподним; runnerполучает конфликтующийsessionlockдоFKпроверки/browser. Неподтверждённыйcleanupостаётсяunknownдаже безPGsession. Уточнение закрываетгонку, новыхIPCкоманд/таблицнет; оценкаосталась9/10. Это follow-up того же обзора, не второйновыйкритик.

## Снимок

- PROCESS-LIFECYCLE-PROPOSAL.md SHA256 3bb854389040a0fc00777a0afab890edec89d85fd8e761368c1bd7f7608778c9
- evidence/PROCESS-LIFECYCLE-PRIMARY-SOURCES.md SHA256 ca05b9408153ea90ee23a171629b1ef91796e35ca32cb11e203011b7e151af2e

[Источники](../evidence/PROCESS-LIFECYCLE-PRIMARY-SOURCES.md). Ограничения: не проверены фактическая очисткаfixedPlaywright, pidfdвцелевомобразе, MacbirthAPI, сроки40/45/60секунд, IPC, concurrency иleasecrashrecovery. Никакого кода/SQL/конфигов/установок/сервисов/процессов агентнеизменял/незапускал. Проект покаНЕПРИНЯТ; оценка не доказывает runtime и не заменяет решение владельца.
