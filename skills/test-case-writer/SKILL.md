---
name: test-case-writer
description: Generates manual test cases as an XML file that strictly mirrors a provided example (assets/test-cases-example.xml) — same element names, order, and nesting. Maps scenarios from test-design-techniques into well-formed XML with steps, test data, and expected results, keeps requirement traceability, and validates the output. Trigger when test cases must be produced in the project XML format.
---

# Test Case Writer

Produce manual test cases as **Zephyr/Jira XML export** (`HRPQA`) that mirrors `assets/test-cases-example.xml` exactly — element names, order, and nesting match 1:1. Only values change.

Формат: корень `<project>`, кейсы в `<testCases><testCase>`, шаги в `<testScript type="steps">`. См. `references/xml-format.md`.

## Golden rule

**Copy the example, then fill values.** Do not redesign the schema, do not rename elements, do not reorder them. If the example has an element you don't have data for, keep the element and leave it empty (or per the example's convention) rather than dropping it.

## Inputs

- Requirements table (with IDs) from `requirements-analysis`.
- Scenario list (priority, polarity, technique) from `test-design-techniques`.

## Per-case content

Every test case must carry:

| Field | XML (Zephyr) |
|-------|----------------|
| Заголовок | `<name>` — префикс `[P1]` при необходимости |
| Цель | `<objective>` |
| Требование | `<issues><issue><key>` — Jira-ключ или `R1` |
| Предусловия | `<precondition>` (CDATA, допускается HTML) |
| Позитив/негатив | `<customField name="Негативный">` → `Да` / `Нет` |
| Smoke | `<customField name="Smoke">` |
| Вид тестирования | `<customField name="Вид тестирования">` |
| Шаг | `<step index="0">` … |
| Действие | `<description>` |
| Ожидаемый результат | `<expectedResult>` |
| Тестовые данные шага | `<testData>` |
| Папка | `<folder>` — путь как в `<folders>` образца |

## Workflow

1. Read `assets/test-cases-example.xml` and `references/xml-format.md` to learn the exact structure.
2. Скопируй шапку `<project>` и `<folders>` из образца; для каждого сценария клонируй один `<testCase>` из образца.
3. Заполни поля; трассируемость `R1` → `<issues>`; шаги — в `<testScript>/<steps>/<step>`.
4. Ensure every requirement ID from the table appears in at least one case (≥1 positive + ≥1 negative).
5. Write the file to `doc-as-code/test-cases/<feature>.xml` (create the directory if missing).
6. **Validate** (mandatory):
   ```bash
   python3 scripts/validate_xml.py doc-as-code/test-cases/<feature>.xml assets/test-cases-example.xml
   ```
   Fix and re-run until it prints `VALID`.
7. Output a traceability table: `Требование → Тест-кейсы`.

## XML hygiene

- Текстовые поля с разметкой — в `<![CDATA[...]]>` как в образце.
- UTF-8; сохраняй `standalone="yes"` в декларации, если есть в образце.
- One logical scenario per case — do not merge unrelated checks.
- Keep Russian text in titles/steps/expected results.

## Rules

- ✅ **MIRROR** the example structure exactly (names, order, nesting).
- ✅ **TRACE** every case to a requirement ID; no orphan cases.
- ✅ **COVER** each requirement with positive + negative + boundary where applicable.
- ✅ **VALIDATE** with `validate_xml.py` before finishing — must be `VALID`.
- ❌ **DO NOT** invent schema elements not present in the example.
- ❌ **DO NOT** drop elements that exist in the example.

## References

- `references/xml-format.md` — element-by-element mapping of case fields → XML nodes.
- `references/examples.md` — filled positive/negative/boundary case examples.
