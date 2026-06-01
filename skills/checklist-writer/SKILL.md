---
name: checklist-writer
description: Produces concise Markdown checklists (smoke and regression) for a feature or a Pull Request, derived from the requirements list. Each item is a fast pass/fail check focused on the highest-risk paths. Trigger when a quick verification list is needed alongside or instead of full test cases.
---

# Checklist Writer

Fast, scannable Markdown checklists — for smoke before deploy and regression around the change. Checklists complement, not replace, the detailed XML test cases.

## Inputs

- Requirements table (IDs, priority) from `requirements-analysis`.
- Scope of change (PR diff) — what to regression-check around.

## Output

Write to `doc-as-code/checklists/<feature>.md` using `assets/checklist-template.md`.

Two sections:
- **Smoke** — minimal "is it alive / core path works" checks (P1 only). Fast, run on every build/deploy.
- **Regression** — checks around the changed area and adjacent functionality that could break.

## Rules

- ✅ Each item is **binary** (pass/fail), one observable check, imperative phrasing.
- ✅ Order by priority — P1 first.
- ✅ Cover both happy path and the main negative/edge paths for the change.
- ✅ Tag each item with the requirement ID for traceability where it applies.
- ✅ Keep it short — a checklist is for speed; deep scenarios belong in XML cases.
- ❌ Do not duplicate full test-case steps here.

## Example

```markdown
## Smoke
- [ ] [R1] Поиск существующего автора возвращает результат (200)
- [ ] [R1] Несуществующий автор → 404, без падения

## Regression
- [ ] [R3] Старые клиенты с отсутствующим `realmName` не ломаются
- [ ] Соседний экран "Список авторов" открывается без ошибок
- [ ] Пустой поиск → пустой список, не 500
```
