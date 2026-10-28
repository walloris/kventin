# Standalone prompt для ревью релиза

Использовать этот текст, если CLI не подхватил skill автоматически. Заменить только `<RELEASE_URL>` и при необходимости блок дополнительного контекста.

```text
Проведи полное доказательное read-only ревью готовности релиза:
<RELEASE_URL>

Дополнительный контекст (если известен):
- целевая ветка: <TARGET_BRANCH_OR_UNKNOWN>
- дата/окно релиза: <RELEASE_DATE_OR_UNKNOWN>
- контур/сборка: <ENV_AND_BUILD_OR_UNKNOWN>
- локальные release gates: <POLICY_PATH_OR_UNKNOWN>

Работай через подключённые MCP Jira, Bitbucket и Confluence. Сначала определи доступные tools и их схемы; не предполагай конкретные имена методов. Ничего не изменяй: не комментируй и не переводи Jira-задачи, не редактируй Confluence, не approve/merge PR и не запускай deployment.

1. Извлеки ключ релиза и получи release issue со всеми полями, links, subtasks, comments, worklogs, attachments, remote/development links и changelog. Обработай всю пагинацию.
2. Построй полный граф состава релиза: прямые issue links, children/subtasks, Story/Task, Bug/Defect/Ошибка, Epic relations и fixVersion. Для каждой задачи укажи основание включения, убери дубли и не зацикливайся на обратных links. Проверь задачи, удалённые из состава по changelog.
3. По каждой задаче получи summary, description, AC, status/resolution, priority/severity, labels, components, versions, custom fields с отображаемыми именами, links, comments, worklogs и development data. Проверь полноту и внутреннюю согласованность.
4. Найди аналитику/требования/функциональные решения/архитектуру во всех Jira fields, comments, attachments и links. Открой Confluence URL/page ID; если явной ссылки нет, выполни узкий поиск по Jira key и точному summary. Не сканируй весь space без основания. Сопоставь Jira AC с Confluence, найди противоречия, пробелы, edge cases, permissions, API/schema/config/migration/feature flags, monitoring и продуктовые analytics events/metrics, если они входят в scope.
5. Для каждой Story/Bug найди реальный Bitbucket PR: сначала explicit development/remote links, затем поиск Jira key в PR title/description, branch и commits. Проверь repository, target/source branch, state/merge commit, approvals, unresolved tasks/comments, conflicts, required CI, commits, changed files и diff. Сверь реализацию с AC и аналитикой.
6. Для каждого бага проверь expected/actual, reproduction, environment/build, severity, release/fixVersion, root cause, fix PR/CI, retest, regression impact, связанные test cases и открытые blockers/follow-ups.
7. Найди содержательный итоговый комментарий тестировщика. Если роль автора недоступна, называй его кандидатом и используй семантические признаки. Определи pass/fail/blocked, environment/build, проверенный scope, test cases/runs, defects, retest и ограничения. Сравни время комментария с последним изменением AC, commit/merge/build и повторным открытием задачи. Комментарий до последнего релевантного изменения считай устаревшим.
8. Для каждой задачи собери цепочку Jira requirement/AC -> Confluence analytics -> PR/diff -> build -> test evidence -> bugs -> release decision и найди разрывы.

Не выдумывай данные. Если источник или проверка недоступны, явно напиши НЕ ПРОВЕРЕНО и причину; не трактуй ошибку поиска как отсутствие сущности. Каждый blocker и риск снабди прямыми ссылками на evidence.

Основной вердикт должен быть ровно одним:
- ГОТОВ: blockers нет и критичные источники полностью проверены;
- ГОТОВ С РИСКАМИ: blockers нет, но есть некритичные/принятые риски;
- НЕ ГОТОВ: есть blocker;
- ПРОВЕРКА НЕПОЛНАЯ: критичный источник недоступен и доказательный вердикт невозможен.

Верни:
1) вердикт и краткое обоснование;
2) покрытие Jira/Bitbucket/Confluence;
3) матрицу всех задач: задача | тип/status | аналитика | PR/CI | тестирование | bugs | итог;
4) blockers;
5) risks;
6) анализ аналитики и противоречий;
7) таблицу комментариев тестировщиков с автором, временем, результатом, свежестью и полнотой;
8) анализ bugs;
9) таблицу PR;
10) что не удалось проверить;
11) приоритизированные следующие действия.
```
