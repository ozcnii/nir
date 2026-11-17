# Настройка работы агентов: независимое ревью и проверка

18 ноября 2026. Работа разрешена пользователем: доска, задачи восьми этапов с зависимостями/AC/проверками, AGENTS и шаблоны, commit/push. Код приложения запрещён. Baseline:283886040df51c51a20d91e18e48fd0dcd57bf60. Использован code-review skill. Максимум три итерации, фактически три (последняя — только публикация/CLI).

## Результат

- Задачи/критерии: **9,2/10**, новый чистый агент workflow_tasks_critic_r1.
- Процесс/контроль: сначала **8,8/10**, после точечной правки **9,4/10**, новый чистый агент workflow_controls_critic_r1.
- Standards: открытых замечаний нет. Spec: единственный P2 устранён. Обе оси достигли >=9/10. Это оценка организации, не реализации приложения.

### Итерация 1

Два агента fork_turns=none, разные оси одного раунда. Старые отзывы/оценки, DECISIONS/WORKING-STATE иnavyki.txt не читали. Оба только читали локальные данные и live GitHub API, не меняли репозиторий/GitHub.

Первый прочитал все live #1–46, AC/verification/dependencies38подзадач и выборочно подробные принятые контракты. Подтверждены покрытие восьми этапов, разделение replay/live/P3, три независимые задачи адаптеров, правила matching, UI/JSON, реальные корпус/разметка/метрики/четыре участника, ранние формальные доказательства и конечный нормоконтроль. Текстовый DAG38узлов/76рёбер безциклов; native выборка связей совпала. FindingsP1/P2/P3нет.

Второй независимо проверил private Project1,46items/восемьparents/38tasks,allBacklog/OPEN/noassignees,семьстатусов,BoardverticalGroupByStatus,видимыйЭтаптаблицы, все38наборовnativeзависимостей/родителей и отсутствиециклов, локальныеtargets/допустимыеIssueForms/RubyYAML. Провереныsame-accountreview/неатомарностьassignee,AC/NOTRUN/SHA/интеграция. RESTподтвердил workflows0/mainprotectedfalse/rulesetsпусты: документы не выдают будущие gates за работающие.

**P2 F1:** CONTRIBUTING допускал трактовку, что первый bootstrapCI T01 требует будущий полный Django/DB/browserконтур8.4 доDone, хотя T02Django зависитотT01. Это создаёт невозможную зависимость.

### Итерация 2

Точечная перепроверка тем же вторым критиком, не второйновыйполныйобзор. CONTRIBUTING/AGENTS/liveREADMEпроекта теперь явноразделяют T01(толькоегоAC/начальныепроверки), T13(полныйнынешнийконтур/negative/migrations/classifier/fail/cancel) и T36(защитаmainпослеT13). Начальныйworkflow не считаетсяfullCI; будущиефункции не требуютсядлязакрытияT01 и не объявляютсяпроверенными. F1закрыт; новых findings нет, оценка9,4/10. Прежниеуспешныепроверкинеповторялись.

### Итерация 3: публикация и CLI

После commit/push c4ff871 подтверждены remote main и все16allowlistblobs. GitHubAPIпоказываетфайлыобеихформ/config/PRtemplate иAGENTS/CONTRIBUTING. Repository.issueTemplatesвернул[]дляYAMLформ; поэтому добавлен явный маршрут ghissuecreate--body-file с title/labels/nativeсвязями/Project иreadback, безнадеждына--template. Тот же критик точечно подтвердил правильностьCLIабзаца и наличиеremoteфайлов, сохранил9,4/10; новых findingsнет. Большеитерацийне проводилось.

Важноеограничение: isBlankIssuesEnabled=trueдлятекущеговладельца при опубликованномblank_issues_enabled:false. ЭтонеиспользуетсякакдоказательствоотключённогоblankUI. Рендерchooser/формвбраузеренепроверен, фактическоеUIотключениеblankнеутверждается. Формыструктурнопроверены/опубликованы; рабочийCLIмаршрутявноописан. НовыйCLIабзацпубликуетсяфинальнымfollow-upcommit иегоremoteblobпроверяетсяотдельно. Это releaseпроверка, нетестприложения.

## Реальные проверки главного агента

[Машинный снимок](../evidence/AGENT-WORKFLOW-SETUP-VERIFICATION.json):

- Созданы8родителей и38рабочихissues,38nativeparentсвязей и76nativeblocked-by; текст/реальныеdepsсогласованы, DAGбезциклов, каждыйtaskимеетобласть/specs/AC/сценарии/доказательства.
- MetadataProjects:46привязанныхissues,всеopen/Backlog,семьстатусов,правильныйЭтап,репозиторийпривязан. Board используетStatusкакколонки,таблицапоказываетЭтап.
- Наодноразовомdraftпроверенывсе7статусов черезmutation+отдельноечтение,затемвозвратBacklogиудалениетолькоэтогоdraft. Рабочиеissuesнеменялистатусы/невыдавалисьзаDone.
- RubyPsychуспешноразбираеттриYAML; reviewerсверилдопустимыеполяIssueFormsсофициальнойсхемой. Тестовыерабочиеissues/PRврепозиторийнепубликовались.
- Локальныессылки/режимработы/неизменностьсоседнихфайлов проверяютсяпередcommit;послеpushпроверяетсяпубликацияновыхmain-файлов. Формы в браузере не проходились; не заявлять ручнуюUIпроверку.

ОшибкиформатаотдельныхGraphQLзапросовпринастройкебылиисправлены; ихнеуспешныепопыткиневыданызауспех. NativeRESTassertionsпервогопрохода завершилисьдоошибкиформатаProjectquery,затемисправленныйProject-onlycheckподтвердилоставшуюсячасть;этоотраженовJSON. Фактическиеуспешныерезультатыненазываютсяapp-тестами.

## Зафиксированные файлы

- AGENTS.md: 5767ba51b6ff19b834585ddaf3ed3f1e1951e1bba77b20419fe33b7d5d2a1027
- CODING.md: 6dd76e525382dbc0f3cb9533c51d1876462237151125610ffc3f3c41fa2f08ad
- CONTRIBUTING.md: 37e29da5da68b870c88ba7f9cd1301434ecf94222a2c0266707f85f273a521e9
- CLAUDE.md: f70052dac4680bea90e3eb6662e3adf216756a0099fe1b99e7710584bde98b7f
- README.md: 7829c219e202d748dc4a30ad617abd1fd6ecbd62d87ac00ab0cafc13986d7775
- .github/ISSUE_TEMPLATE/task.yml: cecd7fb571ba6ff9c320e2c66edf2bce7cd5871b84e2dd0f66badcc35e15bbb1
- .github/ISSUE_TEMPLATE/bug.yml: 7335a6201269bf2a717cce906f5bb503219e59ad6724cc74f3bc0af216fc55ff
- .github/ISSUE_TEMPLATE/config.yml: 1f103c6a9dd07cd13a9a6f17ace6b813f47747eb9cb7e00488cb2073caaf91bb
- .github/pull_request_template.md: c564d0884ed818fecc0f5d9c9d6e14cd9a0fa68753c6d51b2d6005392b437b0c
- bachelor-thesis/IMPLEMENTATION-TRACKER.md: 90fca977732656a6db4efaa9539fcea32f0c6e89cfe4ebc89d0194b58b44f376

## Границы

Реализация/CI/runner/DB/browser/исследование не запускались. Requiredquality/защитаmain — будущаяT36послерабочейT13, анеготоваяавтоматика. Выполнениеправил/назначениеиDone контролируеткоординатор; инструкции/ProjectsнеобеспечиваютатомарныйclaimилиавтоматическуюпроверкувсехAC. Всеapplicationtasksосталисьнезапущенными. Чужойnavyki.txtсохранён. Завершённаянастройка/коммитнеявляютсясдачейВКР.
