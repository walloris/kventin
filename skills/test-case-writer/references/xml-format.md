# XML format mapping — Zephyr / Jira export (HRPQA)

**Источник истины:** `assets/test-cases-example.xml` — экспорт тест-кейсов из Zephyr for Jira (проект `HRPQA`).

Агент **зеркалит** этот XML 1:1: те же теги, порядок, вложенность. Меняются только значения и добавляются новые `<testCase>` внутри `<testCases>`.

## Корневая структура

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<project>
  <projectId>...</projectId>
  <projectKey>HRPQA</projectKey>
  <modelVersion>1.0</modelVersion>
  <jiraVersion>...</jiraVersion>
  <exportDate>...</exportDate>
  <folders>
    <folder fullPath="..." index="N"/>
  </folders>
  <testCases>
    <testCase id="..." key="HRPQA-T...">...</testCase>
  </testCases>
</project>
```

При генерации нового файла:
- Скопируй шапку `<project>` из образца (можно оставить `projectKey` = `HRPQA`).
- Обнови `<exportDate>` на текущую дату UTC.
- Добавь/обнови `<folders>` под фичу.
- Все новые кейсы — только внутри `<testCases>`.

## Структура одного `<testCase>`

```xml
<testCase id="..." key="HRPQA-T...">
  <attachments/>
  <confluencePageLinks/>
  <createdBy>...</createdBy>
  <createdOn>...</createdOn>
  <customFields>...</customFields>
  <estimatedTime>...</estimatedTime>   <!-- опционально, если есть в образце для всех кейсов -->
  <folder><![CDATA[путь/в/дереве/папок]]></folder>
  <issues>
    <issue>
      <key>JIRA-KEY</key>
      <summary><![CDATA[...]]></summary>
    </issue>
  </issues>
  <labels>
    <label><![CDATA[API]]></label>
  </labels>
  <name><![CDATA[Название кейса]]></name>
  <objective><![CDATA[Цель / что проверяем]]></objective>
  <owner>...</owner>
  <precondition><![CDATA[Предусловия]]></precondition>
  <priority><![CDATA[Normal]]></priority>
  <status><![CDATA[Approved]]></status>
  <parameters/>
  <testScript type="steps">
    <steps>
      <step index="0">
        <customFields/>
        <description><![CDATA[Действие]]></description>
        <expectedResult><![CDATA[Ожидаемый результат]]></expectedResult>
        <testData><![CDATA[Данные]]></testData>
      </step>
    </steps>
  </testScript>
  <updatedBy>...</updatedBy>
  <updatedOn>...</updatedOn>
</testCase>
```

Текстовые поля с HTML — оборачивай в `<![CDATA[...]]>` как в образце.

## Маппинг полей агента → XML

| Поле агента | XML |
|-------------|-----|
| ID кейса (внутренний) | атрибут `id` на `<testCase>` (для новых — временный, до импорта в Jira) |
| Ключ Zephyr | атрибут `key` (`HRPQA-T...`) — для новых можно `HRPQA-TNEW-01` или оставить пустым по соглашению команды |
| Заголовок | `<name>` |
| Цель / описание | `<objective>` |
| Предусловия | `<precondition>` |
| Требование / Jira | `<issues><issue><key>R1 или HRM-12345</key><summary>...</summary></issue></issues>` |
| Папка в дереве | `<folder>` (путь как в `<folders>/<folder fullPath=...>`) |
| Приоритет (P1–P4) | маппинг в `<priority>`: P1→High, P2→Normal, … **или** оставить `Normal` и отразить P1 в `<name>` |
| Позитив / негатив | `<customField name="Негативный">` → `Да` / `Нет` |
| Smoke | `<customField name="Smoke">` → `Да` / `Нет` |
| Вид тестирования | `<customField name="Вид тестирования">` → `Новый функционал` / `Регресс` |
| Уровень | `<customField name="Уровень теста">` → `API` / `Web` / … |
| АС | `<customField name="АС">` → автоматизированная система из контекста |
| Команда | `<customField name="Команда">` → из контекста ПП |
| Шаг N | `<step index="N-1">` (индекс с 0) |
| Действие | `<description>` |
| Ожидаемый результат | `<expectedResult>` |
| Тестовые данные шага | `<testData>` |
| Метки | `<labels><label>...</label></labels>` |

## Обязательные customFields (ориентир из образца)

Для каждого нового кейса заполняй набор полей как в образце (те же `name` и `type`):

- Автоматизирован
- АС
- Проверяется при внедрении Cloud
- Команда
- Smoke
- Негативный
- Вид тестирования
- Sigma
- Уровень теста

Если поле неприменимо — возьми значение по умолчанию из ближайшего кейса в образце.

## Правила генерации

1. **Не удаляй** пустые узлы, которые есть в образце (`<attachments/>`, `<parameters/>`, …).
2. **Не меняй** имена тегов и атрибутов (`testScript type="steps"`, `step index="0"`).
3. Новый кейс = клон существующего `<testCase>` из образца + замена значений.
4. Трассируемость требования `R1` → минимум одна запись в `<issues>` или явная ссылка в `<objective>`.
5. После генерации — `validate_xml.py` должен вернуть `VALID`.

## Импорт

Готовый файл предназначен для импорта в Zephyr/Jira (проект HRPQA) или ручной доработки в UI. Агент не вызывает API импорта — только формирует XML.
