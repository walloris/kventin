"""Конфигурация агента-тестировщика."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Страница для тестирования
START_URL = os.getenv("START_URL", "https://example.com")

# --- Провайдер LLM: gigachat | jan ---
# Рекомендуется gigachat для режима «реальный тестировщик» (фазы, оракул, GigaChat лучше держит контекст).
# jan — локальная модель в Jan (OpenAI-совместимый API на http://127.0.0.1:1337)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()
JAN_API_URL = os.getenv("JAN_API_URL", "http://127.0.0.1:1337").rstrip("/")
JAN_API_KEY = os.getenv("JAN_API_KEY", "jan-api-key")
# ID модели в Jan (как в интерфейсе Jan). Для скриншотов нужна vision-модель: Llama-3.2-11B-Vision, Qwen2-VL
JAN_MODEL = os.getenv("JAN_MODEL", "llama-3.2-11b-vision-instruct")

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
GIGACHAT_ENV = os.getenv("GIGACHAT_ENV", "ift").strip().lower()  # "dev" | "ift"
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

# --- Демо-режим: быстрый, активный, зрелищный агент ---
# Включает: сниженные паузы, пропуск оракула, агрессивный промпт, чеклист реже
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")

# Видимость действий (в демо — быстрее клики и короче подсветка)
BROWSER_SLOW_MO = int(os.getenv("BROWSER_SLOW_MO", "50" if DEMO_MODE else "300"))
HIGHLIGHT_DURATION_MS = int(os.getenv("HIGHLIGHT_DURATION_MS", "250" if DEMO_MODE else "800"))
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")
# Размер окна браузера (по умолчанию Full HD — на весь экран)
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))

# Профиль браузера: если задан — запуск с persistent context (сохраняется сертификат, куки, логин)
BROWSER_USER_DATA_DIR = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
if BROWSER_USER_DATA_DIR and not os.path.isabs(BROWSER_USER_DATA_DIR):
    BROWSER_USER_DATA_DIR = str(Path.cwd() / BROWSER_USER_DATA_DIR)

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

# Исключения для дефектов: если в summary/description есть эти фразы — тикет не создаём (404 в консоли и т.д.)
DEFECT_IGNORE_PATTERNS = [
    "404",
    "404 ошибк",
    "ошибк 404",
    "404 в консоли",
    "в консоли 404",
    "консоль",
    "console",
    "failed to load resource",
    "net::err_",
    "favicon",
    "chrome-extension",
    "аналитик",
    "analytics",
    "тестовой сред",
    "флак",
    "flaky",
]

# Чеклист: пауза между шагами (мс), чтобы агент шёл медленнее и по порядку
CHECKLIST_STEP_DELAY_MS = int(os.getenv("CHECKLIST_STEP_DELAY_MS", "500" if DEMO_MODE else "2000"))
# Ожидание загрузки: таймаут networkidle (мс)
WAIT_NETWORK_IDLE_MS = int(os.getenv("WAIT_NETWORK_IDLE_MS", "3000" if DEMO_MODE else "5000"))

# --- Улучшение качества тестирования ---
# В начале сессии запросить у GigaChat тест-план по скриншоту (5–7 шагов)
ENABLE_TEST_PLAN_START = os.getenv("ENABLE_TEST_PLAN_START", "true").lower() in ("1", "true", "yes")
# После важных действий спрашивать GigaChat: достигнут ли ожидаемый результат (оракул)
# В демо-режиме отключён — экономит 2-5с на каждом шаге
ENABLE_ORACLE_AFTER_ACTION = os.getenv("ENABLE_ORACLE_AFTER_ACTION", "false" if DEMO_MODE else "true").lower() in ("1", "true", "yes")
# Перед созданием дефекта — второй проход: «это точно баг?» (снижает ложные тикеты)
ENABLE_SECOND_PASS_BUG = os.getenv("ENABLE_SECOND_PASS_BUG", "false" if DEMO_MODE else "true").lower() in ("1", "true", "yes")
# Повторы при сбое: сколько раз повторять клик/действие при таймауте или not_found
ACTION_RETRY_COUNT = int(os.getenv("ACTION_RETRY_COUNT", "1" if DEMO_MODE else "2"))
# Печатать отчёт сессии каждые N шагов (0 = только в конце при создании дефекта)
SESSION_REPORT_EVERY_N = int(os.getenv("SESSION_REPORT_EVERY_N", "0"))

# Максимальное число шагов агента (0 = бесконечный цикл). При достижении — печатает отчёт и останавливается.
MAX_STEPS = int(os.getenv("MAX_STEPS", "0"))

# Retry при сбое GigaChat (пустой ответ / не JSON): экспоненциальный backoff
LLM_RETRY_COUNT = int(os.getenv("LLM_RETRY_COUNT", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "2.0"))  # секунды

# --- Константы агента (бывшие магические числа) ---
SCROLL_PIXELS = int(os.getenv("SCROLL_PIXELS", "600"))           # пикселей за одну прокрутку
MAX_ACTIONS_IN_MEMORY = int(os.getenv("MAX_ACTIONS_IN_MEMORY", "80"))  # размер истории
MAX_SCROLLS_IN_ROW = int(os.getenv("MAX_SCROLLS_IN_ROW", "2" if DEMO_MODE else "5"))  # в демо — быстрее переключаемся на клики
CONSOLE_LOG_LIMIT = int(os.getenv("CONSOLE_LOG_LIMIT", "150"))    # обрезка логов консоли
NETWORK_LOG_LIMIT = int(os.getenv("NETWORK_LOG_LIMIT", "80"))     # обрезка сетевых ошибок
POST_ACTION_DELAY = float(os.getenv("POST_ACTION_DELAY", "0.15" if DEMO_MODE else "1.5"))  # пауза после действия (сек)
PHASE_STEPS_TO_ADVANCE = int(os.getenv("PHASE_STEPS_TO_ADVANCE", "3" if DEMO_MODE else "5"))  # шагов в фазе до перехода

# --- Продвинутые проверки ---
# Accessibility (a11y) проверки каждые N шагов (0 = отключены)
A11Y_CHECK_EVERY_N = int(os.getenv("A11Y_CHECK_EVERY_N", "20" if DEMO_MODE else "10"))
# Performance-мониторинг каждые N шагов (0 = отключён)
PERF_CHECK_EVERY_N = int(os.getenv("PERF_CHECK_EVERY_N", "25" if DEMO_MODE else "15"))
# Responsive тестирование: после основного прохода переключить на мобильный viewport
ENABLE_RESPONSIVE_TEST = os.getenv("ENABLE_RESPONSIVE_TEST", "true").lower() in ("1", "true", "yes")
RESPONSIVE_VIEWPORTS = [
    {"name": "mobile", "width": 375, "height": 812},
    {"name": "tablet", "width": 768, "height": 1024},
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
