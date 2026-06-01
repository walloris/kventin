# Filled examples — Zephyr / HRPQA export

Минимальный фрагмент нового кейса в формате образца. Полные примеры — в `assets/test-cases-example.xml`.

## Требование

| ID | Требование |
|----|-----------|
| R1 | GET `/web/widgets/data?widgets=tandemy` при грейде &lt; 16 возвращает пустой `data` |

## Новый testCase (фрагмент для вставки в `<testCases>`)

```xml
<testCase id="0" key="HRPQA-TNEW-01">
  <attachments/>
  <confluencePageLinks/>
  <createdBy><![CDATA[senior-manual-qa]]></createdBy>
  <createdOn><![CDATA[2026-06-01 12:00:00 UTC]]></createdOn>
  <customFields>
    <customField name="Автоматизирован" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Готов к автоматизации]]></value>
    </customField>
    <customField name="АС" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[HRP.SmartProfile (Smart профиль) [CI02295205]]]></value>
    </customField>
    <customField name="Проверяется при внедрении Cloud" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Нет]]></value>
    </customField>
    <customField name="Команда" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Профиль сотрудника [61146085]]]></value>
    </customField>
    <customField name="Smoke" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Нет]]></value>
    </customField>
    <customField name="Негативный" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Нет]]></value>
    </customField>
    <customField name="Вид тестирования" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[Новый функционал]]></value>
    </customField>
    <customField name="Sigma" type="CHECKBOX">
      <value><![CDATA[true]]></value>
    </customField>
    <customField name="Уровень теста" type="SINGLE_CHOICE_SELECT_LIST">
      <value><![CDATA[API]]></value>
    </customField>
  </customFields>
  <estimatedTime>300000</estimatedTime>
  <folder><![CDATA[Функциональные тест-кейсы/Каркас/HRP.SmartProfile (Smart профиль) [CI02295205]/Пульс API/app_smart_profile/top600/Тандеми виджет]]></folder>
  <issues>
    <issue>
      <key>R1</key>
      <summary><![CDATA[Тандеми виджет — грейд ниже 16]]></summary>
    </issue>
  </issues>
  <labels>
    <label><![CDATA[API]]></label>
  </labels>
  <name><![CDATA[[P1] GET widgets=tandemy — грейд ниже 16, пустой data]]></name>
  <objective><![CDATA[Проверить, что при грейде руководителя ниже 16 виджет tandemy возвращает пустой объект data.]]></objective>
  <owner><![CDATA[senior-manual-qa]]></owner>
  <precondition><![CDATA[1. У пользователя есть тандеми<br />2. Тогл show_tandemy включен<br />3. Грейд userId ниже 16]]></precondition>
  <priority><![CDATA[Normal]]></priority>
  <status><![CDATA[Approved]]></status>
  <parameters/>
  <testScript type="steps">
    <steps>
      <step index="0">
        <customFields/>
        <description><![CDATA[GET /web/widgets/data?widgets=tandemy&amp;userId=%{userId}]]></description>
        <expectedResult><![CDATA[HTTP 200, success=true, data.code=tandemy, data.data={}]]></expectedResult>
        <testData><![CDATA[userId — UUID пользователя с грейдом &lt; 16]]></testData>
      </step>
    </steps>
  </testScript>
  <updatedBy><![CDATA[senior-manual-qa]]></updatedBy>
  <updatedOn><![CDATA[2026-06-01 12:00:00 UTC]]></updatedOn>
</testCase>
```

## Негативный кейс (фрагмент)

Тот же каркас; отличия:

- `<customField name="Негативный">` → `<value><![CDATA[Да]]></value>`
- `<name>` — явно негативный сценарий
- `<expectedResult>` — код ошибки / пустой ответ / 4xx по ТЗ

## Traceability

| Требование | Ключ / issue |
|------------|----------------|
| R1 | `<issue><key>R1</key>` или ключ Jira `SFILE-11162` из задачи |
