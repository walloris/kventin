# AI-агент тестировщик (Playwright + Local OpenAI-compatible LLM + Jira)

Автономный агент, который непрерывно исследует приложение, анализирует консоль,
сеть и DOM, выполняет проверки, создаёт дефекты в Jira и ретестирует их. Локальная
OpenAI-compatible LLM улучшает выбор и oracle-анализ, но не является точкой отказа:
при её недоступности агент продолжает работу по детерминированной политике.

## Требования

- Python 3.9+
- Локальный OpenAI-compatible endpoint:
  - `http://127.0.0.1:3333/v1/chat/completions`
  - `http://127.0.0.1:3333/v1/models`
- Учётные данные Jira (если нужно заводить дефекты)
- Доступ в интернет только для Jira/установки зависимостей

## Установка

```bash
cd /path/to/kventin
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Для разработки и запуска тестов:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Настройка

1. Скопируйте пример окружения и задайте переменные:

```bash
cp .env.example .env
```

2. В `.env` укажите:

| Переменная | Описание |
|------------|----------|
| `START_URL` | **Обязательно.** URL страницы для тестирования (например `https://example.com`). |
| `LOCAL_LLM_API_URL` | Base URL `http://127.0.0.1:3333/v1` или полный `/v1/chat/completions`. |
| `LOCAL_LLM_MODEL` | ID модели. Если пусто, агент берёт первую модель из `GET /v1/models`. |
| `LOCAL_LLM_API_KEY` | Bearer token для совместимости; по умолчанию `local`. |
| `LLM_REQUEST_TIMEOUT_SEC` | HTTP timeout локальной LLM. |
| `LLM_RETRY_COUNT` | Число попыток для `429`, временных `5xx`, timeout и connection error. |
| `LLM_RETRY_BASE_DELAY` / `LLM_RETRY_MAX_DELAY` | Границы exponential backoff; `Retry-After` имеет приоритет. |
| `LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS` | После скольких ошибок временно перейти только на локальную политику. |
| `JIRA_URL` | URL вашего Jira (например `https://your-company.atlassian.net`). |
| `JIRA_USERNAME` | Логин (username) в Jira. |
| `JIRA_EMAIL` | Email в Jira (если у вас логин по email, например Atlassian Cloud). |
| `JIRA_API_TOKEN` | API-токен (или пароль) для Jira. |
| `JIRA_PROJECT_KEY` | Ключ проекта для создания дефектов (например `PROJ`). |
| `JIRA_VERIFY_SSL` | `0` — отключить проверку SSL для внутренней Jira; по умолчанию `1`. |
| `JIRA_RETRY_COUNT` | Число попыток временно неуспешных Jira REST-запросов. |
| `JIRA_TASK_EXPLORATORY_STEPS` | Бюджет реального exploratory-прогона задачи перед генерацией XML; `0` отключает прогон. |
| `AGENT_CONTINUOUS_RESTART` | Перезапускать аварийно завершившуюся браузерную сессию в бесконечном режиме. |
| `BROWSER_SLOW_MO` | Замедление операций браузера в мс (по умолчанию 300), чтобы было видно действия. |
| `HIGHLIGHT_DURATION_MS` | Пауза после подсветки элемента в мс (по умолчанию 800). |
| `HEADLESS` | `true` — без окна браузера; по умолчанию `false` (окно видно). |

### Локальная LLM

Агент использует только OpenAI-compatible API. Минимальная настройка:

```env
LOCAL_LLM_API_URL=http://127.0.0.1:3333/v1
LOCAL_LLM_MODEL=
LOCAL_LLM_API_KEY=local
LLM_REQUEST_TIMEOUT_SEC=60
```

Если `LOCAL_LLM_MODEL` пустой, агент вызовет:

```bash
curl http://127.0.0.1:3333/v1/models
```

Для анализа скриншотов endpoint должен поддерживать OpenAI vision-формат `image_url` с data URL.

Если локальный `gigacode-proxy` пишет `SELF_SIGNED_CERT_IN_CHAIN` или `self-signed certificate in certificate chain`, ошибка находится внутри Node-прокси при его запросе к upstream. Kventin ходит в локальный `http://127.0.0.1:3333`, TLS там не участвует.

Правильный вариант — запустить прокси с корпоративным CA:

```bash
NODE_EXTRA_CA_CERTS=/path/to/corporate-ca.pem node proxy.js
```

Временный небезопасный обход только для локальной отладки:

```bash
NODE_TLS_REJECT_UNAUTHORIZED=0 node proxy.js
```

## Запуск

Передать URL страницы аргументом:

```bash
python scripts/main.py https://example.com
```

Или без аргумента — тогда используется `START_URL` из `.env`:

```bash
python scripts/main.py
```

По умолчанию агент работает бесконечно. Он исследует страницы того же приложения,
ограничивает глубину и бюджет URL, а после закрытия браузера или страницы supervisor
создаёт новую сессию с bounded backoff.

Для CI можно ограничить прогон:

```bash
HEADLESS=true MAX_STEPS=20 python scripts/main.py https://example.com --json-summary
```

## Поведение агента

1. **Навигация по приложению**
   Агент начинает с переданного URL, исследует внутренние переходы и новые вкладки,
   не уходит на чужие домены и использует лимиты глубины/прогресса против циклов.

2. **Анализ**
   На каждой итерации собираются:
   - **Консоль** — сообщения (log, error, warning);
   - **Сеть** — неуспешные ответы (статус и URL);
   - **DOM** — кнопки и ссылки (тег, текст, id, класс, href).

3. **Local LLM и деградация**
   Контекст отправляется в локальный OpenAI-compatible endpoint. `429`, временные
   `5xx` и transport errors получают мягкие ретраи с jitter и `Retry-After`.
   Circuit breaker не даёт недоступной LLM остановить Playwright.

4. **Jira**
   Детерминированные сигналы (`pageerror`, action failure, значимые `4xx/5xx`)
   не зависят от ответа LLM. Перед созданием применяются структурная, локальная,
   Jira- и семантическая дедупликация. Потерянный ответ create восстанавливается
   поиском тикета перед повтором. Игнорируются:
   - типичные флаки и проблемы тестовой среды;
   - 404 в консоли, `Failed to load resource`, запросы к аналитике, расширениям и т.п.  
   Список игнорируемых паттернов настраивается в `config.py` (`IGNORE_CONSOLE_PATTERNS`, `IGNORE_NETWORK_STATUSES`).

5. **Видимость действий**  
   - Браузер запускается в видимом режиме (`headless=false`) с замедлением (`slow_mo`).  
   - На страницу инжектируется визуальный курсор (красный круг), который перемещается перед кликами.  
   - Перед каждым кликом элемент подсвечивается (`locator.highlight()`), затем выполняется клик.

6. **Модалки, sidebar и dropdown**
   Верхний blocking overlay помечается в DOM как единственная интерактивная область.
   `hidden`, `inert`, `aria-hidden`, disabled, stale и визуально перекрытые элементы
   отклоняются повторным preflight непосредственно перед действием. Закрытие считается
   успешным только после подтверждённого изменения overlay-состояния в DOM.

## Непрерывный daemon

```bash
python scripts/daemon.py
python scripts/daemon.py --once
```

Daemon атомарно блокирует второй экземпляр, независимо обрабатывает очередь Jira-задач
и дефектов в `Ready for QA`/`QA`, повторяет инфраструктурно сорванные ретесты и не
закрывает дефект, если сценарий пуст, неизвестен или выполнен не полностью.

## Структура проекта

```
kventin/
├── config.py             # Конфиг и переменные окружения
├── requirements.txt
├── .env.example
├── README.md
├── agent/                # Код браузерного агента
│   ├── __init__.py
│   ├── core/             # Supervisor, orchestration, memory, resilience, reports
│   ├── actions/          # Candidates, policy, preflight и Playwright adapter
│   ├── browser/          # Overlay state, DOM analysis, network, page objects
│   ├── defects/          # Signals, delivery, Jira, deduplication и retest
│   ├── llm/              # OpenAI-compatible transport и parsing
│   ├── checks/           # Scheduler, a11y, perf, iframe, responsive, visual
│   └── tasks/            # Тестирование Jira-задач
├── scripts/              # Точки входа и разовые утилиты
│   ├── main.py           # python scripts/main.py [URL]
│   ├── daemon.py
│   └── release_checker.py
├── tests/
└── docs/                 # Архитектура и эксплуатационные контракты
```

Подробная схема и инварианты: [`docs/agent_architecture.md`](docs/agent_architecture.md).

## Проверка

```bash
python -m pytest -q
KVENTIN_RUN_BROWSER_TESTS=1 python -m pytest -q tests/test_overlay_browser_integration.py
python -m compileall -q agent scripts tests config.py
```

Browser integration suite проверяет реальный Chromium-сценарий с открытым sidebar,
`aria-hidden/inert` фоном, подтверждаемым закрытием и визуальным перехватом клика.

## Остановка

Прервать бесконечный цикл: `Ctrl+C` в терминале.

## Лицензия

Проект предоставляется «как есть» для внутреннего использования.
