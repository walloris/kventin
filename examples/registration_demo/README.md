# Registration defect lifecycle demo

Демонстрационный стенд проверяет полный жизненный цикл дефекта без внешних сервисов:

1. Релиз `1.0.0-buggy` отвечает `HTTP 500` на валидный `POST /api/register`.
2. Kventin автономно заполняет форму, отправляет её и создаёт `DEMO-*` в локальной debug Jira.
3. Runner разворачивает `1.0.1-fixed` и переводит тикет в `Ready for QA`.
4. Штатный ретест Kventin воспроизводит сохранённый сценарий и закрывает тикет как `Fixed`.

Полный acceptance-прогон:

```bash
.venv/bin/python scripts/run_registration_demo.py
```

Запись цельного demo-видео с действиями агента и переходами debug Jira:

```bash
.venv/bin/python scripts/record_registration_demo.py
```

Готовый ролик сохраняется как `artifacts/registration-demo/kventin-registration-demo.webm`.

Результаты сохраняются в `artifacts/registration-demo/`:

- `debug-jira-discovered.json` — тикет сразу после обнаружения;
- `debug-jira-final.json` — тикет после ретеста;
- `result.json` — машинная сводка всего цикла;
- `layout-desktop.png` и `layout-mobile.png` — проверенные responsive-состояния;
- `agent-session.html` и `agent-session.txt` — отчёт исследовательской сессии.

Для ручного просмотра формы и debug Jira:

```bash
.venv/bin/python examples/registration_demo/server.py --port 8765
```

- форма: `http://127.0.0.1:8765/`
- debug Jira: `http://127.0.0.1:8765/debug/issues`
