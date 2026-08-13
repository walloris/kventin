# Чек-лист доказательного ревью релиза

Этот checklist задаёт общий минимум. Проектные release gates и workflow имеют приоритет.

## 1. Release object и состав

- Release URL/key доступен, type и назначение понятны.
- Status, resolution, release date, environment, components, labels и fixVersion согласованы.
- Определены source/target branch или иной объект поставки.
- Все прямые release links раскрыты.
- Subtasks, Story children, связанные Task и Bugs раскрыты без дублей.
- Для каждой задачи записано основание включения в релиз.
- Changelog проверен на удалённые из состава задачи.
- Нет merged изменений от удалённой задачи в релизной ветке без принятого исключения.

## 2. Полнота Jira-задач

- Summary соответствует description.
- Description содержит проверяемый scope.
- Acceptance criteria атомарны, однозначны и не противоречат друг другу.
- Status/resolution соответствуют фактической готовности.
- Assignee, component, labels и fixVersion заполнены по workflow.
- Blocking/duplicate/depends-on links разрешены или явно приняты.
- Scope changes после тестирования отсутствуют либо повторно протестированы.
- Required custom fields заполнены допустимыми значениями.

## 3. Аналитика, требования и архитектура

- Найдены все явные ссылки из Jira и comments.
- При отсутствии ссылки выполнен узкий поиск по Jira key и точному summary.
- Проверены бизнес-требования, функциональное решение и архитектурные ограничения.
- Версия страницы/дата изменения не старее согласованного scope без объяснения.
- Jira AC и Confluence не противоречат друг другу.
- Определены роли, permissions и ограничения доступа.
- Описаны positive, negative и boundary cases.
- Учтены API contracts, errors, timeouts и backward compatibility.
- Учтены data migrations, defaults, nullable/required fields и rollback.
- Учтены config/feature flags и порядок включения.
- Учтены logging, monitoring, alerts и support diagnostics.
- Если scope содержит продуктовую аналитику: события, параметры, момент отправки, дедупликация, consent/privacy и метрики успеха определены.
- Открытые вопросы и допущения явно отмечены.

## 4. Pull Request и реализация

- У каждой изменяемой Story/Bug есть реальный PR.
- Jira key присутствует в структурированной связи или однозначном PR context.
- PR направлен в правильную target branch.
- PR merged либо находится в допустимом по workflow состоянии.
- Merge conflicts отсутствуют.
- Обязательные approvals получены.
- Unresolved discussions/tasks отсутствуют либо приняты.
- Required CI checks успешны и относятся к актуальному commit.
- Diff соответствует AC и не вносит необъяснённый scope.
- Изменения API/schema/config/migration/flags отражены в Jira/аналитике.
- Нет дополнительных релевантных commits после тестовой сборки.

## 5. Bugs/Defects/Ошибки

- Expected/actual result и steps to reproduce достаточны.
- Environment и affected build/version указаны.
- Severity/priority соответствует влиянию.
- Bug относится к проверяемому release/fixVersion.
- Root cause/аналитическое объяснение заполнено, если требуется workflow.
- Fix PR найден, merged в правильную ветку, CI успешен.
- Retest выполнен на сборке с fix commit.
- Regression scope и связанные test cases указаны.
- Blocker/Critical bugs закрыты или имеют формально принятое исключение.
- Нет открытых linked blockers, дубликатов с более тяжёлым статусом или незавершённых follow-ups.

## 6. Тестовое подтверждение

- Автор подтверждён как тестировщик или отмечен только как кандидат.
- Итог pass/fail/blocked однозначен.
- Указаны environment и build/version.
- Перечислены проверенный scope и исключения.
- Есть ссылки на test cases/test run или достаточное ручное evidence.
- Указаны найденные bugs и их текущий статус.
- Для fixes есть retest result.
- Комментарий новее последнего релевантного code/scope change.
- Более поздний комментарий не отменяет положительный результат.
- Нет фраз о blocker/known issue, противоречащих release status.

## 7. Release-level эксплуатационная готовность

Проверять, если применимо и доступно в источниках:

- deployment order и зависимости;
- database migration и rollback plan;
- feature flag rollout/kill switch;
- monitoring/dashboard/alerts;
- backward/forward compatibility;
- документация поддержки и пользовательские release notes;
- security/privacy approval;
- performance/load evidence;
- владелец выпуска и план действий при деградации.

## 8. Классификация findings

### BLOCKER

- открытый или неподтверждённо исправленный Blocker/Critical;
- tester result = fail/blocked;
- тестовое подтверждение старее последнего существенного code/scope change;
- обязательный PR не найден, не merged, merged не в ту ветку или required CI failed;
- unresolved PR task, явно блокирующий выпуск;
- критичное требование отсутствует в реализации;
- Jira/Confluence/код противоречат друг другу так, что ожидаемое поведение нельзя определить;
- отсутствует обязательный по локальной политике release artifact.

### RISK

- неполный тестовый комментарий при наличии иных подтверждений;
- stale или неполная аналитика без критичного противоречия;
- открытый некритичный bug с понятным impact;
- optional approval/check отсутствует;
- residual risk принят неявно или без владельца;
- слабая traceability при подтверждённой реализации.

### NOT CHECKED

- MCP/source недоступен;
- недостаточно прав;
- tool не поддерживает нужные поля/diff/build/comments;
- пагинация или запрос завершились ошибкой;
- сущность невозможно однозначно сопоставить.

`NOT CHECKED` описывает покрытие и не заменяет `BLOCKER` или `RISK`.
