---
name: test-design-techniques
description: Applies black-box test design techniques to derive minimal-but-complete test cases from a requirement — equivalence partitioning, boundary value analysis, decision tables, pairwise combinations, state transitions, and error guessing. Trigger after requirements analysis and before writing test cases, to decide which scenarios each requirement needs.
---

# Test Design Techniques

Pick the right technique per requirement to get **maximum coverage with minimum cases**. For each requirement from `requirements-analysis`, choose one or more techniques and produce the concrete scenario list that `test-case-writer` turns into XML.

## Technique selector

| Requirement shape | Technique | Why |
|-------------------|-----------|-----|
| Input has valid/invalid ranges | Equivalence Partitioning | One case per class instead of many |
| Input has numeric/length/date limits | Boundary Value Analysis | Bugs cluster at edges |
| Multiple conditions → different outcomes | Decision Table | Covers logic combinations |
| Many independent parameters | Pairwise / All-pairs | Cuts combinatorial explosion |
| Entity has statuses/lifecycle | State Transition | Covers valid + invalid transitions |
| Vague / past defects / integrations | Error Guessing | Catches what specs miss |

## 1. Equivalence Partitioning (классы эквивалентности)

Split input into classes where the system behaves the same; test one value per class.

- Example field `age` (0–120): invalid-low `{ < 0 }`, valid `{ 0..120 }`, invalid-high `{ > 120 }` → 3 cases.

## 2. Boundary Value Analysis (граничные значения)

For each boundary test `min-1, min, min+1, max-1, max, max+1`.

- `age` 0–120 → `-1, 0, 1, 119, 120, 121`.
- Also: empty string, 1 char, max length, max+1; first/last day of period; 0 and overflow for counters.

## 3. Decision Table (таблица решений)

List conditions × actions; one column per rule combination.

| Условие | R1 | R2 | R3 | R4 |
|---------|----|----|----|----|
| Авторизован | Да | Да | Нет | Нет |
| Есть права | Да | Нет | Да | Нет |
| **Результат** | 200 | 403 | 401 | 401 |

Each rule (column) becomes a test case.

## 4. Pairwise (попарное тестирование)

When parameters are independent, cover all **pairs** of values instead of the full Cartesian product. Document which pairs are covered so coverage is auditable.

- 3 params × 3 values = 27 combos → ~9 pairwise cases.

## 5. State Transition (диаграмма состояний)

Model statuses and transitions; cover:
- every valid transition (positive);
- forbidden transitions (negative, e.g. `Closed → InProgress`);
- actions in a state where they are not allowed.

## 6. Error Guessing (предугадывание ошибок)

Add cases for classic failure points: null/empty, special characters, very long input, concurrent actions, expired token/session, network timeout, duplicate submit, wrong content-type, missing required field, leading/trailing spaces, unicode/emoji.

## Output for `test-case-writer`

For each requirement ID, output a scenario list with type and priority:

```markdown
### R1 — поиск автора
- [P1][позитив][эквив.класс] Существующий автор → найден, 200
- [P1][негатив][граница] Несуществующий автор → 404
- [P2][негатив][error guessing] Пустая строка поиска → 200, пустой список
- [P2][негатив][граница] Запрос > 255 символов → 400
```

## Rules

- **ALWAYS** ensure each requirement has ≥1 positive and ≥1 negative scenario.
- **ALWAYS** apply boundary analysis where any limit/range exists.
- **PREFER** fewer, higher-value cases — use equivalence classes to avoid redundant cases.
- **MARK** each scenario with priority `[P1..P4]`, polarity `[позитив|негатив]`, and technique.
- **MAKE** pairwise coverage explicit so it can be reviewed.
