# Новый независимый критик — журнал, состояние и перезапуск

18 ноября 2026. fresh_journal_restart_critic, fork_turns=none, code-review skill. Current-state review проектных документов. Прежниеreviews/DECISIONS/WORKING-STATE иnavyki.txt не открывал. Проверены4.3/5.4/5.5/runtime/lifecycle/firstlaunch по необходимости и первичныеPython/Linux/Dockerссылки; реализацияотсутствует.

## Первичный результат

**8/10, одинP2 Spec.** Синхронный logging/fsync и snapshotподlock могли заблокироватьstop/status/TERM/KILL/deadline. ReaderPIPE сам недостаточен: его зависание наdiskwrite может прекратитьdrain и заблокироватьтакжерunner. Нужно было отделитьконтрольпроцессов отфайловогоI/O/locks/EOFиограничитьпередачу/завершениеwriter.

Standards: других нарушений/лишнихframework не найдено. ОстальныеграницыSpec согласованы: solewriter, stateнеliveness/committruth, hostfencing+ownerlock, manualfreshrestart, понятныеcorruption/retention, protectedunknown безложнойквоты, сохранениеcommits, доступ/минимизациячувствительныхданных.

## Правка и точечная проверка

Controlотделён отwriter, отдельныйPIPEreader и одинwriter вlauncher; ограниченныйnativeбуфер/put_nowait, ACKdeadline5секунд, памятьбезI/Olocks. Переполнение/позднийACK/невыходящийwriter не останавливаютstop/deadline. storagefaultнеисчезаетотlateACK, новыхrunнет, fallbackпоарендам. Включёнбудущийсценарийhungfsync/fullqueue/lateACK иboundedshutdown. Разделеныошибкидоreplace ипослеreplace/directoryfsync.

Тот же критик точечно перечитал §3, добавленный будущийсценарий иevidence. **P2закрыто на уровнепроектирования. Итог9/10, достаточно длясогласования, новыхзамечанийвпроверенномобъёменет.** Этоfollow-up того же обзора, не второйчистыйкритик.

## Снимок после правки

- JOURNAL-STATE-RESTART-PROPOSAL.md SHA256 8c117759f60837e0c60569df6495d353b29641e4235543571969bfa2b609e55e
- evidence/JOURNAL-STATE-PRIMARY-SOURCES.md SHA256 691545ab19a6c9077e0f893c9f325fc90469d3acb164b2fb86535c51bd9352db

[Первичныеисточники](../evidence/JOURNAL-STATE-PRIMARY-SOURCES.md). Не проверены фактическиеPIPE/ACK/diskfull/IOhang, threadshutdown/atexit, restartfencing, volumeUID/сохранность, rotation/чтение иконкуренция. Программа/конфигурации/SQL/тома/сервисы не создавались/не запускались; агентфайлыредактироватьнеимелправаинеизменял. ПредложениепокаНЕПРИНЯТО. Оценка относится кпроектномуконтракту, анеработающейсистеме.
