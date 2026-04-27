"""Конфигурация агента-тестировщика."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Страница для тестирования
START_URL = os.getenv("START_URL", "https://example.com")

# --- Провайдер LLM: gigachat | jan | openai | anthropic | ollama ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()
# Jan (локальная модель, OpenAI-совместимый API)
JAN_API_URL = os.getenv("JAN_API_URL", "http://127.0.0.1:1337").rstrip("/")
JAN_API_KEY = os.getenv("JAN_API_KEY", "jan-api-key")
JAN_MODEL = os.getenv("JAN_MODEL", "llama-3.2-11b-vision-instruct")
# OpenAI (gpt-4o, gpt-4o-mini с vision)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
# Anthropic (Claude с vision)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
# Ollama (локально: llava, llama3.2-vision)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llava")

# GigaChat (Keycloak password grant + gateway, как в рабочем примере)
GIGACHAT_TOKEN_HEADER = os.getenv("GIGACHAT_TOKEN_HEADER", "")  # опционально: готовый "Bearer eyJ..."
GIGACHAT_API_URL = os.getenv("GIGACHAT_API_URL", "")  # единый URL чата (если не заданы _DEV/_IFT)
GIGACHAT_TOKEN_URL = os.getenv("GIGACHAT_TOKEN_URL", "")  # единый URL токена
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")
GIGACHAT_AUTHORIZATION_KEY = os.getenv("GIGACHAT_AUTHORIZATION_KEY", "")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "fakeuser")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "")
GIGACHAT_USERNAME = os.getenv("GIGACHAT_USERNAME", "")
GIGACHAT_PASSWORD = os.getenv("GIGACHAT_PASSWORD", "")
GIGACHAT_ENV = os.getenv("GIGACHAT_ENV", "dev").strip().lower()  # "dev" | "ift"
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "0") == "1"
# Person ID для Keycloak (обязательно для password grant через x-hrp-person-id)
GIGACHAT_PERSON_ID_DEV = os.getenv("GIGACHAT_PERSON_ID_DEV", "4c36eb04-0920-4449-9e07-ca4a68f80eef")
GIGACHAT_PERSON_ID_IFT = os.getenv("GIGACHAT_PERSON_ID_IFT", "91ed8888-bff4-4d61-a72d-310db2eeaa37")
# URL по стендам (если не заданы — подставляются дефолты под Sberbank HR)
GIGACHAT_TOKEN_URL_DEV = os.getenv("GIGACHAT_TOKEN_URL_DEV", "https://hr-dev.sberbank.ru/auth/realms/PAOSberbank/protocol/openid-connect/token")
GIGACHAT_TOKEN_URL_IFT = os.getenv("GIGACHAT_TOKEN_URL_IFT", "https://hr-ift.sberbank.ru/auth/realms/PAOSberbank/protocol/openid-connect/token")
GIGACHAT_API_URL_DEV = os.getenv("GIGACHAT_API_URL_DEV", "https://hr-dev.sberbank.ru/api-web/neurosearchbar/api/v1/gigachat/completion")
GIGACHAT_API_URL_IFT = os.getenv("GIGACHAT_API_URL_IFT", "https://hr-ift.sberbank.ru/api-web/neurosearchbar/api/v1/gigachat/completion")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")

# Jira (логин: username или email — в зависимости от типа Jira)
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")  # для Atlassian Cloud часто используют email
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")
# Тип задачи при создании (на время тестирования — Task, потом можно Bug)
JIRA_ISSUE_TYPE = os.getenv("JIRA_ISSUE_TYPE", "Task")
# Assignee (назначить дефект на пользователя): username для Server, accountId для Cloud, или пусто = текущий пользователь
JIRA_ASSIGNEE = os.getenv("JIRA_ASSIGNEE", "").strip()

# Видимость действий
BROWSER_SLOW_MO = int(os.getenv("BROWSER_SLOW_MO", "300"))
HIGHLIGHT_DURATION_MS = int(os.getenv("HIGHLIGHT_DURATION_MS", "800"))
# В CI (GITHUB_ACTIONS, GITLAB_CI, CI=1) по умолчанию headless, если не задан HEADLESS вручную
_headless_env = os.getenv("HEADLESS", "").lower()
_ci_env = bool(os.getenv("CI") or os.getenv("GITHUB_ACTIONS") or os.getenv("GITLAB_CI"))
HEADLESS = _headless_env in ("1", "true", "yes") or (_ci_env and _headless_env != "false" and _headless_env != "0")
# Размер окна браузера (по умолчанию Full HD — на весь экран)
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))

# Профиль браузера: если задан — запуск с persistent context (сохраняется сертификат, куки, логин)
BROWSER_USER_DATA_DIR = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
if BROWSER_USER_DATA_DIR and not os.path.isabs(BROWSER_USER_DATA_DIR):
    BROWSER_USER_DATA_DIR = str(Path.cwd() / BROWSER_USER_DATA_DIR)

# Подавить диалог выбора сертификата (чтобы запускать агента в скрытом/headless режиме).
# Добавляются аргументы Chromium: --ignore-certificate-errors и на macOS --use-mock-keychain.
# 1=вкл всегда, 0=выкл; при HEADLESS или CI по умолчанию вкл.
_suppress_cert_env = os.getenv("BROWSER_SUPPRESS_CERT_PROMPT", "").lower()
BROWSER_SUPPRESS_CERT_PROMPT = _suppress_cert_env in ("1", "true", "yes") or (
    (HEADLESS or bool(os.getenv("CI") or os.getenv("GITHUB_ACTIONS") or os.getenv("GITLAB_CI")))
    and _suppress_cert_env not in ("0", "false", "no")
)
# Доп. аргументы Chromium через запятую, например: --use-mock-keychain,--ignore-certificate-errors
BROWSER_CHROMIUM_ARGS_STR = os.getenv("BROWSER_CHROMIUM_ARGS", "").strip()
BROWSER_CHROMIUM_ARGS = [a.strip() for a in BROWSER_CHROMIUM_ARGS_STR.split(",") if a.strip()]

# Клиентский сертификат (убирает окно выбора): задать origin(ы) и путь к .pfx или .pem+.key.
# Браузер сам подставит сертификат — диалог не показывается.
BROWSER_CLIENT_CERT_ORIGIN = os.getenv("BROWSER_CLIENT_CERT_ORIGIN", "").strip()
# Несколько origin через запятую (один и тот же сертификат для всех)
BROWSER_CLIENT_CERT_ORIGINS = [o.strip() for o in os.getenv("BROWSER_CLIENT_CERT_ORIGINS", "").split(",") if o.strip()]
BROWSER_CLIENT_CERT_PFX_PATH = os.getenv("BROWSER_CLIENT_CERT_PFX_PATH", "").strip()
BROWSER_CLIENT_CERT_PASSPHRASE = os.getenv("BROWSER_CLIENT_CERT_PASSPHRASE", "").strip()
BROWSER_CLIENT_CERT_CERT_PATH = os.getenv("BROWSER_CLIENT_CERT_CERT_PATH", "").strip()
BROWSER_CLIENT_CERT_KEY_PATH = os.getenv("BROWSER_CLIENT_CERT_KEY_PATH", "").strip()
# Авто-выбор сертификата по паттерну URL (без файла сертификата): политика Chrome.
# Задать один или несколько паттернов через запятую, например https://[*.]example.com
# Работает только при BROWSER_USER_DATA_DIR: в профиль пишется политика (Chrome подхватывает при запуске).
BROWSER_AUTO_SELECT_CERT_PATTERNS = [p.strip() for p in os.getenv("BROWSER_AUTO_SELECT_CERT_PATTERNS", "").split(",") if p.strip()]

# Игнорируемые паттерны (флаки, тестовая среда, 404 в консоли и т.д.)
IGNORE_CONSOLE_PATTERNS = [
    "404",
    "net::ERR_",
    "Failed to load resource",
    "favicon",
    "chrome-extension",
    "localhost",
    "127.0.0.1",
    "analytics",
    "gtm",
    "google-analytics",
    "hotjar",
    "sentry",
    "ads.",
    "adservice",
]
IGNORE_NETWORK_STATUSES = {404}  # можно расширить: 502, 503 для тестовой среды

# Отдельные паттерны игнора сетевых запросов (URL). Не путать с IGNORE_CONSOLE_PATTERNS.
IGNORE_NETWORK_URL_PATTERNS = [
    "favicon",
    "analytics",
    "gtm",
    "google-analytics",
    "hotjar",
    "sentry",
    "ads.",
    "adservice",
    "chrome-extension",
    "localhost",
    "127.0.0.1",
]

# Исключения для дефектов: если в summary/description есть эти фразы — тикет не создаём.
# ВАЖНО: паттерны должны быть СПЕЦИФИЧНЫМИ. Не клади сюда короткие слова вроде
# "console", "консоль", "404" — они встречаются почти в любом адекватном описании
# дефекта (URL, заголовки, фразы вроде «новые ошибки консоли») и приведут к тому,
# что все дефекты будут молча отбрасываться.
DEFECT_IGNORE_PATTERNS = [
    "404 в консоли",
    "в консоли 404",
    "favicon",
    "chrome-extension",
    "moz-extension",
    "тестовой среды",
    "флаки",
    "flaky test",
    "is this a flaky",
]

# Чеклист: пауза между шагами (мс)
CHECKLIST_STEP_DELAY_MS = int(os.getenv("CHECKLIST_STEP_DELAY_MS", "2000"))
# Ожидание загрузки: таймаут networkidle (мс)
WAIT_NETWORK_IDLE_MS = int(os.getenv("WAIT_NETWORK_IDLE_MS", "5000"))

# --- Улучшение качества тестирования ---
ENABLE_TEST_PLAN_START = os.getenv("ENABLE_TEST_PLAN_START", "true").lower() in ("1", "true", "yes")
ENABLE_ORACLE_AFTER_ACTION = os.getenv("ENABLE_ORACLE_AFTER_ACTION", "true").lower() in ("1", "true", "yes")
ENABLE_SECOND_PASS_BUG = os.getenv("ENABLE_SECOND_PASS_BUG", "true").lower() in ("1", "true", "yes")
ACTION_RETRY_COUNT = int(os.getenv("ACTION_RETRY_COUNT", "2"))
# Печатать отчёт сессии каждые N шагов (0 = только в конце при создании дефекта)
SESSION_REPORT_EVERY_N = int(os.getenv("SESSION_REPORT_EVERY_N", "0"))
# Сохранять отчёт в файл(ы) во время работы: 0 = только в конце, 1 = каждый шаг (по умолчанию).
SESSION_REPORT_SAVE_EVERY_N = int(os.getenv("SESSION_REPORT_SAVE_EVERY_N", "1"))

# Максимальное число шагов агента (0 = бесконечный цикл). При достижении — печатает отчёт и останавливается.
MAX_STEPS = int(os.getenv("MAX_STEPS", "0"))

# Retry при сбое GigaChat (пустой ответ / не JSON): экспоненциальный backoff
LLM_RETRY_COUNT = int(os.getenv("LLM_RETRY_COUNT", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "2.0"))  # секунды
# Если GigaChat не ответил за N секунд — берём fast action (не зависаем)
GIGACHAT_RESPONSE_TIMEOUT_SEC = int(os.getenv("GIGACHAT_RESPONSE_TIMEOUT_SEC", "20"))
# Circuit breaker: после N таймаутов подряд не вызывать GigaChat 60 сек (0 = отключить)
GIGACHAT_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS = int(os.getenv("GIGACHAT_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS", "3"))
GIGACHAT_CIRCUIT_BREAKER_COOLDOWN_SEC = int(os.getenv("GIGACHAT_CIRCUIT_BREAKER_COOLDOWN_SEC", "60"))
# Таймаут на одно действие Playwright (клик, fill, wait), мс
ACTION_TIMEOUT_MS = int(os.getenv("ACTION_TIMEOUT_MS", "10000"))
# Путь к файлу итогового отчёта сессии (пусто = только в консоль)
# Путь к текстовому отчёту сессии (пусто = только в консоль). По умолчанию — для теста отчёта.
SESSION_REPORT_PATH = os.getenv("SESSION_REPORT_PATH", "./session_report.txt").strip()
# Путь к HTML-отчёту (красивый отчёт в браузере). По умолчанию — для теста.
SESSION_REPORT_HTML_PATH = os.getenv("SESSION_REPORT_HTML_PATH", "./session_report.html").strip()
# JSONL-лог шагов (одна строка JSON на шаг)
SESSION_REPORT_JSONL = os.getenv("SESSION_REPORT_JSONL", "").strip()
# Сохранять скриншот после каждого шага в папку (путь к папке)
SAVE_STEP_SCREENSHOTS_DIR = os.getenv("SAVE_STEP_SCREENSHOTS_DIR", "").strip()
# Оракул только при изменении экрана или новых ошибках (экономия вызовов GigaChat)
ORACLE_ON_VISUAL_OR_ERROR = os.getenv("ORACLE_ON_VISUAL_OR_ERROR", "true").lower() in ("1", "true", "yes")

# --- Константы агента (бывшие магические числа) ---
SCROLL_PIXELS = int(os.getenv("SCROLL_PIXELS", "600"))           # пикселей за одну прокрутку
MAX_ACTIONS_IN_MEMORY = int(os.getenv("MAX_ACTIONS_IN_MEMORY", "80"))  # размер истории
MAX_SCROLLS_IN_ROW = int(os.getenv("MAX_SCROLLS_IN_ROW", "5"))
CONSOLE_LOG_LIMIT = int(os.getenv("CONSOLE_LOG_LIMIT", "150"))
NETWORK_LOG_LIMIT = int(os.getenv("NETWORK_LOG_LIMIT", "80"))
POST_ACTION_DELAY = float(os.getenv("POST_ACTION_DELAY", "1.5"))
PHASE_STEPS_TO_ADVANCE = int(os.getenv("PHASE_STEPS_TO_ADVANCE", "5"))

# Бюджет на URL: сколько шагов может «сгореть» без новых протестированных
# элементов на одном паттерне URL, прежде чем агент принудительно вернётся
# на стартовую страницу (см. AgentMemory.should_force_back_to_start).
URL_BUDGET_NO_PROGRESS = int(os.getenv("URL_BUDGET_NO_PROGRESS", "25"))

# --- Продвинутые проверки ---
A11Y_CHECK_EVERY_N = int(os.getenv("A11Y_CHECK_EVERY_N", "10"))
PERF_CHECK_EVERY_N = int(os.getenv("PERF_CHECK_EVERY_N", "15"))
# Responsive тестирование: после основного прохода переключить на мобильный viewport
ENABLE_RESPONSIVE_TEST = os.getenv("ENABLE_RESPONSIVE_TEST", "true").lower() in ("1", "true", "yes")
RESPONSIVE_VIEWPORTS = [
    {"name": "mobile", "width": 375, "height": 812, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
    {"name": "tablet", "width": 768, "height": 1024, "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
]
# Session persistence: проверять сохранение состояния после reload каждые N шагов (0 = отключено)
SESSION_PERSIST_CHECK_EVERY_N = int(os.getenv("SESSION_PERSIST_CHECK_EVERY_N", "20"))
# Self-healing: после N неудачных действий подряд — мета-рефлексия
SELF_HEAL_AFTER_FAILURES = int(os.getenv("SELF_HEAL_AFTER_FAILURES", "4"))
# Сценарные цепочки: запрашивать у GigaChat цепочку из N действий
ENABLE_SCENARIO_CHAINS = os.getenv("ENABLE_SCENARIO_CHAINS", "true").lower() in ("1", "true", "yes")
SCENARIO_CHAIN_LENGTH = int(os.getenv("SCENARIO_CHAIN_LENGTH", "4"))
# iframe: тестировать содержимое iframe
ENABLE_IFRAME_TESTING = os.getenv("ENABLE_IFRAME_TESTING", "true").lower() in ("1", "true", "yes")

# Критические сценарии: список шагов, которые агент должен выполнить в первую очередь
# Формат: через запятую текстовые подсказки, например "Открыть меню, Клик Контакты, Заполнить форму"
CRITICAL_FLOW_STEPS = [s.strip() for s in os.getenv("CRITICAL_FLOW_STEPS", "").split(",") if s.strip()]

# Cookie/баннер: селекторы или текст кнопок для закрытия (принять cookies, согласен и т.д.)
# Через запятую, например "Принять,Accept,Согласен,ОК,Понятно,cookie,Cookies"
COOKIE_BANNER_BUTTON_TEXTS = [s.strip() for s in os.getenv("COOKIE_BANNER_BUTTON_TEXTS", "Принять,Accept,Согласен,ОК,Понятно,Все cookies,cookie,Cookies,Разрешить,Соглашаюсь").split(",") if s.strip()]

# Оверлеи, которые НЕ часть приложения: чат, поддержка, виджеты + служебный UI агента (чат с LLM, Kventin).
# Паттерны в id/class/aria-label/тексте (нижний регистр). Через запятую.
OVERLAY_IGNORE_PATTERNS = [s.strip().lower() for s in os.getenv("OVERLAY_IGNORE_PATTERNS", "chat,чат,support,поддержк,help,консультант,jivo,intercom,crisp,drift,tawk,livechat,live-chat,widget-chat,chat-widget,feedback,обратн,звонок,callback,kventin,agent-llm,agent-banner,диалог с llm,ai-тестировщик,gigachat").split(",") if s.strip()]

# --- Навигация и покрытие ---
# Максимальная глубина переходов от start_url (0 = без лимита). Не уходить глубже N кликов.
MAX_NAVIGATION_DEPTH = int(os.getenv("MAX_NAVIGATION_DEPTH", "0"))
# После exploratory — обход непосещённых ссылок внутри домена (0 = отключено)
ENABLE_FULL_SITEMAP_CRAWL = os.getenv("ENABLE_FULL_SITEMAP_CRAWL", "false").lower() in ("1", "true", "yes")

# --- Аутентификация ---
# URL страницы логина, логин/пароль, селектор кнопки входа (пусто = без автологина)
AUTH_URL = os.getenv("AUTH_URL", "").strip()
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "").strip()
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "").strip()
AUTH_SUBMIT_SELECTOR = os.getenv("AUTH_SUBMIT_SELECTOR", "").strip()  # например button[type=submit] или "Войти"

# --- Состояние и восстановление ---
# Путь к файлу для сохранения cookies+localStorage (пусто = не сохранять)
SESSION_STATE_SAVE_PATH = os.getenv("SESSION_STATE_SAVE_PATH", "").strip()
# Восстановить состояние из файла перед стартом (если файл есть)
SESSION_STATE_RESTORE_PATH = os.getenv("SESSION_STATE_RESTORE_PATH", "").strip()

# --- Видеозапись и отчёты ---
# Папка для записи видео сессии (Playwright record_video_dir)
RECORD_VIDEO_DIR = os.getenv("RECORD_VIDEO_DIR", "").strip()
# Путь к baseline JSONL для сравнения регрессий (загрузить предыдущий прогон)
SESSION_BASELINE_JSONL = os.getenv("SESSION_BASELINE_JSONL", "").strip()
# Экспорт в JUnit XML (путь к файлу)
JUNIT_REPORT_PATH = os.getenv("JUNIT_REPORT_PATH", "").strip()

# --- Расширенные проверки ---
# Учитывать Shadow DOM при сборе элементов (get_dom_summary)
ENABLE_SHADOW_DOM = os.getenv("ENABLE_SHADOW_DOM", "true").lower() in ("1", "true", "yes")
# Проверять битые ссылки (href/src) на странице каждые N шагов (0 = отключено)
BROKEN_LINKS_CHECK_EVERY_N = int(os.getenv("BROKEN_LINKS_CHECK_EVERY_N", "0"))
# Логировать предупреждения и deprecation из консоли в отчёт
ENABLE_CONSOLE_WARNINGS_IN_REPORT = os.getenv("ENABLE_CONSOLE_WARNINGS_IN_REPORT", "true").lower() in ("1", "true", "yes")
# Детектировать mixed content (HTTPS-страница загружает HTTP-ресурсы)
ENABLE_MIXED_CONTENT_CHECK = os.getenv("ENABLE_MIXED_CONTENT_CHECK", "true").lower() in ("1", "true", "yes")
# Мониторинг WebSocket (ошибки, неожиданное закрытие)
ENABLE_WEBSOCKET_MONITOR = os.getenv("ENABLE_WEBSOCKET_MONITOR", "true").lower() in ("1", "true", "yes")

# --- Загрузка файлов ---
# Путь к тестовому файлу для input type=file (пусто = не тестировать загрузку)
TEST_UPLOAD_FILE_PATH = os.getenv("TEST_UPLOAD_FILE_PATH", "").strip()

# --- Браузер: движок Playwright ---
# chromium | firefox | webkit
BROWSER_ENGINE = os.getenv("BROWSER_ENGINE", "chromium").strip().lower() or "chromium"
if BROWSER_ENGINE not in ("chromium", "firefox", "webkit"):
    BROWSER_ENGINE = "chromium"

# --- Visual regression baseline ---
# Папка для эталонных скриншотов (URL -> hash). Пусто = не сравнивать с baseline.
VISUAL_BASELINE_DIR = os.getenv("VISUAL_BASELINE_DIR", "").strip()
# Порог изменения в % для детекции регрессии (0–100)
VISUAL_REGRESSION_THRESHOLD_PCT = float(os.getenv("VISUAL_REGRESSION_THRESHOLD_PCT", "5.0"))

# --- Экспорт сессии в Playwright-скрипт ---
PLAYWRIGHT_EXPORT_PATH = os.getenv("PLAYWRIGHT_EXPORT_PATH", "").strip()

# --- API-интеркепт (сбор XHR/fetch) ---
ENABLE_API_INTERCEPT = os.getenv("ENABLE_API_INTERCEPT", "true").lower() in ("1", "true", "yes")
API_LOG_MAX = int(os.getenv("API_LOG_MAX", "100"))

# --- Flakiness: повторные прогоны перед дефектом ---
# Сколько раз перезапустить действие при сбое для оценки flakiness (0 = не перезапускать, 2–5 типично)
FLAKINESS_RERUN_COUNT = int(os.getenv("FLAKINESS_RERUN_COUNT", "0"))

# --- Спецификация теста (YAML): сценарии до автономного прохода ---
TEST_SPEC_YAML_PATH = os.getenv("TEST_SPEC_YAML_PATH", "").strip()

# --- Утверждения на естественном языке (проверка через LLM после шагов) ---
# Список утверждений через запятую, например: "После логина видна фамилия пользователя"
NL_ASSERTIONS = [s.strip() for s in os.getenv("NL_ASSERTIONS", "").split(",") if s.strip()]

# --- Несколько стартовых URL (через запятую; приоритет над START_URL) ---
START_URLS = [s.strip() for s in os.getenv("START_URLS", "").split(",") if s.strip()]

# --- DOM diff: считать изменение DOM после действия (нет изменения = возможный баг) ---
ENABLE_DOM_DIFF_AFTER_ACTION = os.getenv("ENABLE_DOM_DIFF_AFTER_ACTION", "true").lower() in ("1", "true", "yes")

# --- CI/CD ---
# Режим CI (авто-определение или KVENTIN_CI=1)
_ci_detected = bool(os.getenv("CI") or os.getenv("GITHUB_ACTIONS") or os.getenv("GITLAB_CI"))
CI_MODE = os.getenv("KVENTIN_CI", "1" if _ci_detected else "0").lower() in ("1", "true", "yes")
# Порог дефектов для exit code: если создано дефектов > N — exit 1 (0 = падать при любом дефекте, -1 = не падать)
FAIL_ON_DEFECTS = int(os.getenv("FAIL_ON_DEFECTS", "-1"))
