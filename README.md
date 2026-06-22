# AI-агент тестировщик (Playwright + Local OpenAI-compatible LLM + Jira)

Автономный агент, который **бесконечно** тестирует одну переданную страницу:

---

## Как запушить код на GitHub

1. Открой **Терминал** (в Cursor: меню Terminal → New Terminal или `` Ctrl+` ``).
2. Перейди в папку проекта и выполни одну команду:

```bash
cd /Users/walloris/Documents/kventin && git add -A && git status
```

Если видишь список файлов — потом выполни:

```bash
git commit -m "обновление" && git push -u origin main
```

Если при `git push` спросит **логин** — введи свой GitHub-логин. Если спросит **пароль** — в GitHub пароль больше не подходит: нужен **токен**. Как получить: зайди на [github.com → Settings → Developer settings → Personal access tokens](https://github.com/settings/tokens), нажми «Generate new token», отметь `repo`, скопируй токен и вставь его в терминал вместо пароля.

---

Автономный агент, который **бесконечно** тестирует одну переданную страницу: анализирует консоль, сеть и DOM, советуется с локальной **OpenAI-compatible LLM** для принятия решений и при необходимости создаёт дефекты в **Jira** через API. Флаки и типичные проблемы тестовой среды (404 в консоли и т.п.) **игнорируются**. Все действия агента **видимы**: браузер в режиме с замедлением, визуальный курсор на странице и подсветка элементов перед кликом.

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
| `JIRA_URL` | URL вашего Jira (например `https://your-company.atlassian.net`). |
| `JIRA_USERNAME` | Логин (username) в Jira. |
| `JIRA_EMAIL` | Email в Jira (если у вас логин по email, например Atlassian Cloud). |
| `JIRA_API_TOKEN` | API-токен (или пароль) для Jira. |
| `JIRA_PROJECT_KEY` | Ключ проекта для создания дефектов (например `PROJ`). |
| `JIRA_VERIFY_SSL` | `0` — отключить проверку SSL для внутренней Jira; по умолчанию `1`. |
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

## Запуск

Передать URL страницы аргументом:

```bash
python main.py https://example.com
```

Или без аргумента — тогда используется `START_URL` из `.env`:

```bash
python main.py
```

Агент работает **бесконечно**: в цикле анализирует страницу, спрашивает локальную LLM «что делать дальше», выполняет клики или создаёт дефекты в Jira, при переходе по ссылке проверяет открытие и возвращается на переданную страницу.

Для CI можно ограничить прогон:

```bash
HEADLESS=true MAX_STEPS=20 python main.py https://example.com --json-summary
```

## Поведение агента

1. **Одна страница**  
   Агент тестирует только переданный URL. Если по клику происходит переход по ссылке (URL меняется), он лишь проверяет, что страница открылась, и возвращается назад на исходный URL.

2. **Анализ**  
   На каждой итерации собираются:
   - **Консоль** — сообщения (log, error, warning);
   - **Сеть** — неуспешные ответы (статус и URL);
   - **DOM** — кнопки и ссылки (тег, текст, id, класс, href).

3. **Local LLM**
   Контекст (консоль, сеть, DOM и скриншот) отправляется в локальный OpenAI-compatible endpoint. Агент задаёт вопрос: что кликнуть следующим или есть ли дефект для Jira. Действия выполняются по ответу.

4. **Jira**  
   Дефекты создаются по API только когда LLM указывает на реальный баг. Игнорируются:
   - типичные флаки и проблемы тестовой среды;
   - 404 в консоли, `Failed to load resource`, запросы к аналитике, расширениям и т.п.  
   Список игнорируемых паттернов настраивается в `config.py` (`IGNORE_CONSOLE_PATTERNS`, `IGNORE_NETWORK_STATUSES`).

5. **Видимость действий**  
   - Браузер запускается в видимом режиме (`headless=false`) с замедлением (`slow_mo`).  
   - На страницу инжектируется визуальный курсор (красный круг), который перемещается перед кликами.  
   - Перед каждым кликом элемент подсвечивается (`locator.highlight()`), затем выполняется клик.

## Структура проекта

```
kventin/
├── main.py              # Точка входа (python main.py [URL])
├── config.py             # Конфиг и переменные окружения
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── __init__.py
    ├── agent.py          # Основной цикл агента
    ├── llm_client.py # Фасад LLM для агента
    ├── local_openai_client.py # Локальный OpenAI-compatible клиент
    ├── jira_client.py    # Создание дефектов в Jira
    ├── page_analyzer.py  # Сбор консоли, сети, DOM
    └── visible_actions.py # Курсор и подсветка элементов
```

## Остановка

Прервать бесконечный цикл: `Ctrl+C` в терминале.

## Лицензия

Проект предоставляется «как есть» для внутреннего использования.
