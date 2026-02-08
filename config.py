"""Конфигурация агента-тестировщика."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Страница для тестирования
START_URL = os.getenv("START_URL", "https://example.com")

# GigaChat
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "")

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
