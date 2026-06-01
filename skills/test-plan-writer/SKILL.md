---
name: test-plan-writer
description: Produces a concise Markdown test plan for a change — scope, out-of-scope, risks, environments and test data, roles, entry/exit criteria, schedule and effort estimate, and a link to the test cases and checklists. Trigger when a change needs a documented testing approach before execution.
---

# Test Plan Writer

A short, practical Markdown test plan that ties together requirements, test cases, and checklists, and states when testing can start and when it is considered done.

## Inputs

- Requirements table from `requirements-analysis`.
- The generated test cases (XML) and checklists (paths).
- Scope of change from the PR.

## Output

Write to `doc-as-code/test-plan/<feature>.md` using `assets/test-plan-template.md`.

## Required sections

| Section | Content |
|---------|---------|
| Объект и цель | Что тестируем и зачем (фича, JIRA-KEY, PR) |
| В scope / вне scope | Явные границы — что проверяем и что НЕ проверяем |
| Подход | Уровни (функц., регресс, негатив, граничные), применённые техники тест-дизайна |
| Риски | Риски качества + меры (что покрываем плотнее) |
| Окружения и данные | Стенды (DEV/QA/IFT/UAT), учётки, тестовые данные |
| Критерии входа | Что должно быть готово, чтобы начать тестирование |
| Критерии выхода | Когда тестирование считается завершённым (все P1 пройдены, нет открытых блокеров, покрытие AC = 100%) |
| Оценка | Трудозатраты на прохождение (часы/дни) |
| Артефакты | Ссылки на XML-кейсы и чек-листы |

## Entry / Exit criteria (default)

**Вход:** сборка задеплоена на стенд; тестовые данные подготовлены; требования/AC зафиксированы; нет блокирующих дефектов окружения.

**Выход:** 100% AC покрыты кейсами и пройдены; все P1/P2 выполнены; нет открытых дефектов severity Blocker/Critical; результаты зафиксированы; чек-лист регрессии пройден.

## Rules

- ✅ Keep it 1–2 pages — actionable, not ceremonial.
- ✅ Make exit criteria measurable (counts, severities, % coverage).
- ✅ Reference the actual artifact paths (cases, checklists).
- ✅ State out-of-scope explicitly to avoid silent gaps.
- ❌ Do not restate every test case — link to them.
