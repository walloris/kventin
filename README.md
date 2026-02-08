# AI-агент тестировщик (Playwright + GigaChat + Jira)

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

Автономный агент, который **бесконечно** тестирует одну переданную страницу: анализирует консоль, сеть и DOM, советуется с **GigaChat** для принятия решений и при необходимости создаёт дефекты в **Jira** через API. Флаки и типичные проблемы тестовой среды (404 в консоли и т.п.) **игнорируются**. Все действия агента **видимы**: браузер в режиме с замедлением, визуальный курсор на странице и подсветка элементов перед кликом.

## Требования

- Python 3.9+
- Учётные данные GigaChat API (для консультаций)
- Учётные данные Jira (если нужно заводить дефекты)
- Доступ в интернет

## Установка

```bash
cd /path/to/kventin
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
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
| `GIGACHAT_CREDENTIALS` | Строка авторизации GigaChat (Base64 от `client_id:client_secret`) или используйте `GIGACHAT_CLIENT_ID` и `GIGACHAT_CLIENT_SECRET`. |
| `JIRA_URL` | URL вашего Jira (например `https://your-company.atlassian.net`). |
| `JIRA_USERNAME` | Логин (username) в Jira. |
| `JIRA_EMAIL` | Email в Jira (если у вас логин по email, например Atlassian Cloud). |
| `JIRA_API_TOKEN` | API-токен (или пароль) для Jira. |
| `JIRA_PROJECT_KEY` | Ключ проекта для создания дефектов (например `PROJ`). |
| `BROWSER_SLOW_MO` | Замедление операций браузера в мс (по умолчанию 300), чтобы было видно действия. |
| `HIGHLIGHT_DURATION_MS` | Пауза после подсветки элемента в мс (по умолчанию 800). |
| `HEADLESS` | `true` — без окна браузера; по умолчанию `false` (окно видно). |

### Локальная модель в Jan (Mac M4 32GB и др.)

Вместо GigaChat можно использовать **локальную модель** в [Jan](https://jan.ai/) (OpenAI-совместимый API).

**Важно:** чтобы агент «видел» скриншоты страницы, в Jan должна быть загружена **vision-модель** (multimodal). Обычные текстовые модели (Vikhr, Qwen2.5-7B без VL) скриншоты не обрабатывают.

1. Установи [Jan](https://github.com/janhq/jan/releases) и запусти приложение.
2. Скачай **vision-модель** (для Mac Mini M4 32GB):
   - **Llama 3.2 11B Vision Instruct** (GGUF, квант Q4_K_M ~6 GB) — мультиязычная, хорошо работает с экранами. В Jan/Hugging Face ищи `Llama-3.2-11B-Vision-Instruct-GGUF`.
   - **Qwen2-VL-7B** (именно VL — vision, не Qwen2.5 7B текстовый) — мультиязычная, понимает изображения.
   - Альтернативы: **LLaVA**, **Pixtral** (если есть в каталоге Jan).
3. В Jan: **Settings → Local API Server** → задай API Key (например `jan-api-key`) → **Start Server**. В логах: `JAN API listening at http://127.0.0.1:1337`.
4. В `.env` задай:
   ```
   LLM_PROVIDER=jan
   JAN_API_URL=http://127.0.0.1:1337
   JAN_API_KEY=jan-api-key
   JAN_MODEL=<ID модели в Jan>
   ```
   `JAN_MODEL` — точный ID модели, как в Jan (например `llama-3.2-11b-vision-instruct-q4_k_m` или как в списке после загрузки).

Только vision-модели получают скриншот. Текстовые (Vikhr, Qwen2.5 без VL) работают без картинки — агент опирается на DOM и контекст.

Получить GigaChat API: [developers.sber.ru — GigaChat](https://developers.sber.ru/portal/products/gigachat-api).

## Запуск

Передать URL страницы аргументом:

```bash
python main.py https://example.com
```

Или без аргумента — тогда используется `START_URL` из `.env`:

```bash
python main.py
```

Агент работает **бесконечно**: в цикле анализирует страницу, спрашивает GigaChat «что делать дальше», выполняет клики или создаёт дефекты в Jira, при переходе по ссылке проверяет открытие и возвращается на переданную страницу.

## Поведение агента

1. **Одна страница**  
   Агент тестирует только переданный URL. Если по клику происходит переход по ссылке (URL меняется), он лишь проверяет, что страница открылась, и возвращается назад на исходный URL.

2. **Анализ**  
   На каждой итерации собираются:
   - **Консоль** — сообщения (log, error, warning);
   - **Сеть** — неуспешные ответы (статус и URL);
   - **DOM** — кнопки и ссылки (тег, текст, id, класс, href).

3. **GigaChat**  
   Контекст (консоль, сеть, DOM) отправляется в GigaChat. Агент задаёт вопрос: что кликнуть следующим или есть ли дефект для Jira. Действия выполняются по ответу.

4. **Jira**  
   Дефекты создаются по API только когда GigaChat указывает на реальный баг. Игнорируются:
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
    ├── gigachat_client.py # Запросы к GigaChat API
    ├── jira_client.py    # Создание дефектов в Jira
    ├── page_analyzer.py  # Сбор консоли, сети, DOM
    └── visible_actions.py # Курсор и подсветка элементов
```

## Остановка

Прервать бесконечный цикл: `Ctrl+C` в терминале.

## Лицензия

Проект предоставляется «как есть» для внутреннего использования.
