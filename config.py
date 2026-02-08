"""Конфигурация агента-тестировщика."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Страница для тестирования
START_URL = os.getenv("START_URL", "https://example.com")

# GigaChat (как в твоём проекте: token_header, gateway URL, OAuth или password grant)
GIGACHAT_TOKEN_HEADER = os.getenv("GIGACHAT_TOKEN_HEADER", "")  # "Bearer eyJ..." — готовый токен
GIGACHAT_API_URL = os.getenv("GIGACHAT_API_URL", "")  # URL чата (например внутренний gateway)
GIGACHAT_TOKEN_URL = os.getenv("GIGACHAT_TOKEN_URL", "")  # URL для получения токена (OAuth)
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max:latest")
GIGACHAT_AUTHORIZATION_KEY = os.getenv("GIGACHAT_AUTHORIZATION_KEY", "")  # Base64(client_id:client_secret)
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "")
GIGACHAT_USERNAME = os.getenv("GIGACHAT_USERNAME", "")
GIGACHAT_PASSWORD = os.getenv("GIGACHAT_PASSWORD", "")
GIGACHAT_ENV = os.getenv("GIGACHAT_ENV", "ift")  # "dev" или "ift" — для выбора token_url / api_url
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "0") == "1"
# Опционально: разные URL для dev/ift (если заданы — переопределяют GIGACHAT_TOKEN_URL / GIGACHAT_API_URL)
GIGACHAT_TOKEN_URL_DEV = os.getenv("GIGACHAT_TOKEN_URL_DEV", "")
GIGACHAT_TOKEN_URL_IFT = os.getenv("GIGACHAT_TOKEN_URL_IFT", "")
GIGACHAT_API_URL_DEV = os.getenv("GIGACHAT_API_URL_DEV", "")
GIGACHAT_API_URL_IFT = os.getenv("GIGACHAT_API_URL_IFT", "")
# Совместимость со старым способом (публичный API)
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")

# Jira
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")

# Видимость действий
BROWSER_SLOW_MO = int(os.getenv("BROWSER_SLOW_MO", "300"))
HIGHLIGHT_DURATION_MS = int(os.getenv("HIGHLIGHT_DURATION_MS", "800"))
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")

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
