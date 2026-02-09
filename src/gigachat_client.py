"""
Клиент GigaChat API для консультаций агента.
Поддержка как в твоём проекте: token_header (готовый Bearer), свой gateway (api_url/token_url),
OAuth (authorization_key или client_id+client_secret), password grant (username, password, client_id).
"""
import base64
import logging
import os
import re
import time
import uuid
from typing import Optional, List, Dict, Any

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

LOG = logging.getLogger("GigaChat")
LOG.setLevel(logging.DEBUG)
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[GigaChat] %(levelname)s %(message)s"))
    LOG.addHandler(h)


def _mask(s: str, show_tail: int = 8) -> str:
    """Скрыть середину строки для логов (первые 4 + ... + последние show_tail)."""
    if not s or len(s) <= 12:
        return "***" if s else "(пусто)"
    return s[:4] + "…" + s[-show_tail:] if len(s) > 12 else "***"

# Публичный API (если не заданы свои URL)
DEFAULT_TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


def get_gigachat_token(env: str) -> Optional[str]:
    """
    Получение токена методом из твоего проекта: grant_type=password, username, client_id (без пароля).
    URL берётся из config: token_url_dev / token_url_ift по env.
    """
    try:
        from config import (
            GIGACHAT_USERNAME,
            GIGACHAT_CLIENT_ID,
            GIGACHAT_TOKEN_URL,
            GIGACHAT_TOKEN_URL_DEV,
            GIGACHAT_TOKEN_URL_IFT,
        )
    except ImportError:
        GIGACHAT_USERNAME = os.getenv("GIGACHAT_USERNAME", "")
        GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "")
        GIGACHAT_TOKEN_URL = os.getenv("GIGACHAT_TOKEN_URL", "")
        GIGACHAT_TOKEN_URL_DEV = os.getenv("GIGACHAT_TOKEN_URL_DEV", "")
        GIGACHAT_TOKEN_URL_IFT = os.getenv("GIGACHAT_TOKEN_URL_IFT", "")

    url = (GIGACHAT_TOKEN_URL_IFT if env == "ift" else GIGACHAT_TOKEN_URL_DEV) or GIGACHAT_TOKEN_URL or _config("TOKEN_URL") or DEFAULT_TOKEN_URL
    if not GIGACHAT_USERNAME or not GIGACHAT_CLIENT_ID:
        LOG.warning("get_gigachat_token: не заданы username или client_id")
        return None

    LOG.info("🔗 Попытка подключения к: %s", url)
    payload = {
        "grant_type": "password",
        "username": GIGACHAT_USERNAME,
        "client_id": GIGACHAT_CLIENT_ID,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(
            url,
            data=payload,
            headers=headers,
            verify=False,
            timeout=30,
        )
        if response.status_code == 200:
            LOG.info("✅ Токен получен успешно")
            return response.json().get("access_token")
        LOG.error("❌ HTTP ошибка: %s - %s", response.status_code, response.text)
        return None
    except requests.exceptions.ConnectionError as e:
        LOG.error("❌ Ошибка подключения: %s", e)
        LOG.error("💡 Возможные причины: сервер недоступен, проблемы с сетью, блокировка файрволом")
        return None
    except requests.exceptions.Timeout as e:
        LOG.error("❌ Таймаут подключения: %s", e)
        return None
    except Exception as e:
        LOG.error("❌ Неожиданная ошибка: %s", e)
        return None


def _config(key: str, default: str = "") -> str:
    try:
        from config import (
            GIGACHAT_TOKEN_HEADER,
            GIGACHAT_API_URL,
            GIGACHAT_TOKEN_URL,
            GIGACHAT_MODEL,
            GIGACHAT_AUTHORIZATION_KEY,
            GIGACHAT_CLIENT_ID,
            GIGACHAT_CLIENT_SECRET,
            GIGACHAT_USERNAME,
            GIGACHAT_PASSWORD,
            GIGACHAT_ENV,
            GIGACHAT_VERIFY_SSL,
            GIGACHAT_TOKEN_URL_DEV,
            GIGACHAT_TOKEN_URL_IFT,
            GIGACHAT_API_URL_DEV,
            GIGACHAT_API_URL_IFT,
            GIGACHAT_CREDENTIALS,
        )
    except ImportError:
        return os.getenv(f"GIGACHAT_{key}", default) if default is not None else os.getenv(f"GIGACHAT_{key}", "")

    m = {
        "TOKEN_HEADER": GIGACHAT_TOKEN_HEADER,
        "API_URL": GIGACHAT_API_URL,
        "TOKEN_URL": GIGACHAT_TOKEN_URL,
        "MODEL": GIGACHAT_MODEL,
        "AUTHORIZATION_KEY": GIGACHAT_AUTHORIZATION_KEY,
        "CLIENT_ID": GIGACHAT_CLIENT_ID,
        "CLIENT_SECRET": GIGACHAT_CLIENT_SECRET,
        "USERNAME": GIGACHAT_USERNAME,
        "PASSWORD": GIGACHAT_PASSWORD,
        "ENV": GIGACHAT_ENV,
        "CREDENTIALS": GIGACHAT_CREDENTIALS,
    }
    v = m.get(key, default or "")

    if key == "API_URL" and not v:
        env = m.get("ENV", "ift")
        v = GIGACHAT_API_URL_IFT if env == "ift" else GIGACHAT_API_URL_DEV
    if key == "TOKEN_URL" and not v:
        env = m.get("ENV", "ift")
        v = GIGACHAT_TOKEN_URL_IFT if env == "ift" else GIGACHAT_TOKEN_URL_DEV

    return v or default or ""


class GigaChatClient:
    """Клиент как в твоём проекте: token_header, OAuth, password grant, свой gateway."""

    def __init__(self, env: Optional[str] = None):
        self.env = env or _config("ENV") or "ift"
        try:
            from config import GIGACHAT_VERIFY_SSL
            self.verify_ssl = bool(GIGACHAT_VERIFY_SSL)
        except ImportError:
            self.verify_ssl = os.getenv("GIGACHAT_VERIFY_SSL", "0").lower() in ("1", "true", "yes")

        self.token_header = _config("TOKEN_HEADER").strip()  # "Bearer eyJ..."
        self.model = _config("MODEL") or "GigaChat-2-Max:latest"
        self.authorization_key = _config("AUTHORIZATION_KEY")
        self.client_id = _config("CLIENT_ID")
        self.client_secret = _config("CLIENT_SECRET")
        self.username = _config("USERNAME")
        self.password = _config("PASSWORD")
        self.credentials = _config("CREDENTIALS")  # старый способ: одна строка client_id:client_secret

        self.token_url = _config("TOKEN_URL")
        self.api_url = _config("API_URL")
        if not self.token_url:
            self.token_url = DEFAULT_TOKEN_URL
        if not self.api_url:
            self.api_url = DEFAULT_API_URL

        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

        # Лог конфига для дебага кредов (без вывода секретов целиком)
        auth_type = "token_header" if self.token_header else ("get_gigachat_token" if (self.username and self.client_id) else ("oauth" if self._basic_key() else ("password_grant" if (self.username and self.password and self.client_id) else "none")))
        LOG.debug(
            "config: api_url=%s token_url=%s model=%s env=%s auth=%s verify_ssl=%s",
            self.api_url[:60] + "..." if len(self.api_url) > 60 else self.api_url,
            self.token_url[:60] + "..." if len(self.token_url) > 60 else self.token_url,
            self.model,
            self.env,
            auth_type,
            self.verify_ssl,
        )
        if self.token_header:
            LOG.debug("token_header: %s", _mask(self.token_header.strip()[:80], show_tail=6))
        if self._basic_key() and not self.token_header:
            LOG.debug("basic_key (oauth): %s", _mask(self._basic_key(), show_tail=4))

    def _normalize_model(self, model: str) -> str:
        if not model:
            return self.model or "GigaChat-2-Max:latest"
        return model if ":latest" in model else f"{model}:latest"

    def _basic_key(self) -> str:
        if self.authorization_key:
            return self.authorization_key.strip()
        if self.client_id and self.client_secret:
            raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
            return base64.b64encode(raw).decode("ascii")
        if self.credentials:
            if ":" in self.credentials and not self.credentials.startswith("eyJ"):
                return base64.b64encode(self.credentials.encode("utf-8")).decode("ascii")
        return ""

    def _get_token_oauth(self) -> Optional[str]:
        basic_key = self._basic_key()
        if not basic_key:
            LOG.debug("oauth: пропуск — нет basic_key (authorization_key / client_id+secret / credentials)")
            return None
        rq_uid = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rq_uid,
            "Authorization": f"Basic {basic_key}",
        }
        data = f"scope={self.scope}"
        LOG.info("oauth: POST %s scope=%s RqUID=%s Authorization=Basic %s", self.token_url, self.scope, rq_uid, _mask(basic_key, show_tail=4))
        try:
            r = requests.post(
                self.token_url,
                data=data,
                headers=headers,
                verify=self.verify_ssl,
                timeout=30,
            )
        except Exception as e:
            LOG.exception("oauth: ошибка подключения к token_url: %s", e)
            return None
        LOG.info("oauth: ответ %s body_len=%s", r.status_code, len(r.text))
        if r.status_code != 200:
            LOG.error("oauth: ответ %s %s", r.status_code, r.text[:800])
            return None
        try:
            payload = r.json()
        except Exception as ex:
            LOG.error("oauth: ответ не JSON: %s", ex)
            return None
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 1800) or 1800)
        if token:
            self.access_token = token
            self.token_expires_at = time.time() + expires_in
            LOG.info("oauth: токен получен, expires_in=%s, token=%s", expires_in, _mask(token, show_tail=6))
            return token
        LOG.warning("oauth: в ответе нет access_token: %s", str(payload)[:400])
        return None

    def _get_token_password_grant(self) -> Optional[str]:
        if not (self.username and self.password and self.client_id):
            LOG.debug("password_grant: пропуск — нет username/password/client_id")
            return None
        LOG.info("password_grant: POST %s username=%s client_id=%s", self.token_url, self.username, self.client_id)
        try:
            payload = {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": self.client_id,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
            r = requests.post(
                self.token_url,
                data=payload,
                headers=headers,
                verify=self.verify_ssl,
                timeout=30,
            )
            LOG.info("password_grant: ответ %s body_len=%s", r.status_code, len(r.text))
            if r.status_code != 200:
                LOG.error("password_grant: ответ %s %s", r.status_code, r.text[:500])
                return None
            data = r.json()
            self.access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 1800) or 1800)
            self.token_expires_at = time.time() + expires_in
            LOG.info("password_grant: токен получен, expires_in=%s token=%s", expires_in, _mask(self.access_token or "", show_tail=6))
            return self.access_token
        except Exception as e:
            LOG.exception("password_grant: ошибка: %s", e)
            return None

    def _get_token(self) -> Optional[str]:
        if self.token_header:
            s = self.token_header.strip()
            tok = s[7:].strip() if s.lower().startswith("bearer ") else s
            LOG.debug("get_token: используем token_header, token=%s", _mask(tok, show_tail=6))
            return tok
        if self.access_token and time.time() < self.token_expires_at - 60:
            LOG.debug("get_token: кэшированный токен до %s", time.strftime("%H:%M:%S", time.localtime(self.token_expires_at)))
            return self.access_token
        # Твой метод: username + client_id (без пароля)
        if self.username and self.client_id:
            LOG.debug("get_token: get_gigachat_token(env=%s)...", self.env)
            token = get_gigachat_token(self.env)
            if token:
                self.access_token = token
                self.token_expires_at = time.time() + 1800  # 30 мин по умолчанию
                return token
        LOG.debug("get_token: запрос oauth...")
        token = self._get_token_oauth()
        if token:
            return token
        LOG.debug("get_token: запрос password_grant (username+password+client_id)...")
        token = self._get_token_password_grant()
        if not token:
            LOG.error("get_token: не удалось получить токен (token_header/get_gigachat_token/oauth/password_grant)")
        return token

    def _files_url(self) -> str:
        """Вычислить URL для загрузки файлов из api_url (/chat/completions → /files)."""
        base = self.api_url
        if "/chat/completions" in base:
            return base.replace("/chat/completions", "/files")
        # Fallback: добавить /files к базовому URL
        return base.rstrip("/").rsplit("/", 1)[0] + "/files"

    def _upload_screenshot(self, screenshot_bytes: bytes) -> Optional[str]:
        """
        Загрузить скриншот через GigaChat /files API.
        Возвращает file_id или None.
        """
        token = self._get_token()
        if not token:
            return None
        files_url = self._files_url()
        headers = {
            "Authorization": f"Bearer {token}",
        }
        LOG.info("upload_screenshot: POST %s (%d bytes)", files_url, len(screenshot_bytes))
        try:
            r = requests.post(
                files_url,
                headers=headers,
                files={"file": ("screenshot.jpg", screenshot_bytes, "image/jpeg")},
                data={"purpose": "general"},
                verify=self.verify_ssl,
                timeout=60,
            )
            LOG.info("upload_screenshot: ответ %s body_len=%s", r.status_code, len(r.text))
            if r.status_code in (200, 201):
                data = r.json()
                file_id = data.get("id") or data.get("file_id")
                if file_id:
                    LOG.info("upload_screenshot: file_id=%s", file_id)
                    return file_id
                LOG.warning("upload_screenshot: нет id в ответе: %s", str(data)[:300])
            else:
                LOG.warning("upload_screenshot: ошибка %s %s", r.status_code, r.text[:300])
        except Exception as e:
            LOG.warning("upload_screenshot: ошибка: %s", e)
        return None

    def chat(self, messages: List[Dict[str, Any]]) -> str:
        token = self._get_token()
        if not token:
            LOG.error("chat: нет токена, запрос отменён")
            return ""

        model = self._normalize_model(self.model)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "top_p": 0.9,
            "safe_mode": False,
            "profanity_check": False,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        last_msg = messages[-1] if messages else {}
        user_len = len(last_msg.get("content", "")) if isinstance(last_msg.get("content"), str) else 0
        has_image = "<img" in (last_msg.get("content", "") if isinstance(last_msg.get("content"), str) else "")
        LOG.info("chat: POST %s model=%s msgs=%s user_len=%s has_image=%s", self.api_url, model, len(messages), user_len, has_image)
        try:
            r = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                verify=self.verify_ssl,
                timeout=120,
            )
            LOG.info("chat: ответ %s body_len=%s", r.status_code, len(r.text))
            if r.status_code != 200:
                LOG.error("chat: ответ %s %s", r.status_code, r.text[:1200])
                return ""
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                LOG.warning("chat: в ответе нет choices: %s", str(data)[:500])
                return ""
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            LOG.info("chat: content_len=%s", len(content))
            LOG.debug("chat: content (head 500): %s", content[:500] if content else "(пусто)")
            return content
        except Exception as e:
            LOG.exception("chat: ошибка запроса: %s", e)
            return ""

    def chat_with_screenshot(self, text_prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> str:
        """
        Отправить промпт со скриншотом в GigaChat.
        Стратегия:
          1) Загрузить скриншот через /files → получить file_id → <img src="file_id"> в тексте
          2) Если /files не работает → inline <img src="data:image/jpeg;base64,..."> в тексте
          3) Если и это 400 → текст без картинки (fallback)
        """
        system = system or "Ты — AI-тестировщик. Отвечай на русском. Кратко, структурированно."

        if not screenshot_b64:
            return self.query(text_prompt, system=system)

        # Сжать скриншот (JPEG, уменьшить размер) для снижения payload
        screenshot_bytes = self._compress_screenshot(screenshot_b64)

        # --- Стратегия 1: загрузить через /files ---
        file_id = self._upload_screenshot(screenshot_bytes)
        if file_id:
            LOG.info("chat_with_screenshot: используем file_id=%s", file_id)
            user_content = f"{text_prompt}\n<img src=\"{file_id}\">"
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            result = self.chat(messages)
            if result:
                return result
            LOG.warning("chat_with_screenshot: file_id не сработал, пробуем inline base64")

        # --- Стратегия 2: inline base64 <img> тег в тексте ---
        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        user_content_inline = f"{text_prompt}\n<img src=\"data:image/jpeg;base64,{img_b64}\">"
        messages_inline = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content_inline},
        ]
        LOG.info("chat_with_screenshot: пробуем inline base64 (img_len=%d)", len(img_b64))
        result = self.chat(messages_inline)
        if result:
            return result

        # --- Стратегия 3: fallback — только текст ---
        LOG.warning("chat_with_screenshot: изображение не поддерживается, fallback на текст")
        return self.query(text_prompt, system=system)

    @staticmethod
    def _compress_screenshot(screenshot_b64: str) -> bytes:
        """Сжать скриншот: PNG base64 → JPEG bytes, уменьшить до 1280px по ширине."""
        raw_png = base64.b64decode(screenshot_b64)
        try:
            from io import BytesIO
            from PIL import Image
            img = Image.open(BytesIO(raw_png))
            # Уменьшить до 1280px по ширине
            if img.width > 1280:
                ratio = 1280 / img.width
                new_size = (1280, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            # Конвертировать в RGB (JPEG не поддерживает alpha)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=70, optimize=True)
            LOG.info("compress_screenshot: %d bytes PNG → %d bytes JPEG", len(raw_png), buf.tell())
            return buf.getvalue()
        except ImportError:
            LOG.warning("compress_screenshot: Pillow не установлен, отправляем PNG как есть")
            return raw_png
        except Exception as e:
            LOG.warning("compress_screenshot: ошибка сжатия: %s, отправляем PNG", e)
            return raw_png

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        system = system or "Отвечай на русском. Кратко и по делу."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        return self.chat(messages)


# Глобальный клиент (ленивая инициализация): GigaChat или Jan (локальная модель)
_client: Optional[Any] = None


def _get_client():
    """Вернуть клиент LLM: всегда GigaChat."""
    global _client
    if _client is None:
        _client = GigaChatClient()
        LOG.info("Using LLM: GigaChat")
    return _client


def init_gigachat_connection() -> bool:
    """
    Инициализировать соединение с GigaChat до запуска браузера: получить клиент и токен,
    при необходимости отправить минимальный запрос для проверки доступности API.
    Возвращает True, если соединение установлено (токен получен), иначе False.
    """
    try:
        client = _get_client()
        token = client._get_token()
        if not token:
            LOG.warning("init_gigachat_connection: не удалось получить токен")
            return False
        LOG.info("GigaChat: соединение инициализировано (токен получен)")
        # Минимальный запрос для проверки доступности API
        out = client.query("Ответь одним словом: ок", system="Ты отвечаешь только одним словом.")
        if out and len(out.strip()) > 0:
            LOG.info("GigaChat: API доступен")
        return True
    except Exception as e:
        LOG.exception("init_gigachat_connection: %s", e)
        return False


def ask_gigachat(prompt: str, **kwargs: Any) -> Optional[str]:
    """Один запрос к GigaChat. Поддерживаются все способы авторизации из конфига."""
    result = _get_client().query(prompt, system=kwargs.get("system"))
    return result if result else None


def consult_agent(context: str, question: str) -> Optional[str]:
    """Задать GigaChat вопрос в контексте тестирования (без скриншота)."""
    full_prompt = f"""Контекст:
{context}

Вопрос: {question}"""
    return ask_gigachat(full_prompt)


def _llm_call_with_retry(prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> Optional[str]:
    """Вызов GigaChat с retry и экспоненциальным backoff при пустом ответе."""
    try:
        from config import LLM_RETRY_COUNT, LLM_RETRY_BASE_DELAY
    except ImportError:
        LLM_RETRY_COUNT, LLM_RETRY_BASE_DELAY = 3, 2.0

    last_result = None
    for attempt in range(max(1, LLM_RETRY_COUNT)):
        result = _get_client().chat_with_screenshot(prompt, screenshot_b64=screenshot_b64, system=system)
        if result and result.strip():
            return result
        last_result = result
        if attempt < LLM_RETRY_COUNT - 1:
            delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
            LOG.warning("LLM retry %d/%d — пустой ответ, пауза %.1fс", attempt + 1, LLM_RETRY_COUNT, delay)
            time.sleep(delay)
    return last_result


# Белый список допустимых действий (для валидации ответа GigaChat)
VALID_ACTIONS = {"click", "type", "scroll", "hover", "close_modal", "select_option", "press_key", "check_defect", "explore"}


def validate_llm_action(action: dict) -> dict:
    """
    Валидация и нормализация JSON-ответа GigaChat.
    - Приводит action к нижнему регистру
    - Исправляет русские синонимы (кликнуть → click и т.д.)
    - Проверяет, что selector не пуст для действий, где он обязателен
    """
    act = (action.get("action") or "").strip().lower()
    # Русские синонимы
    rus_map = {
        "кликнуть": "click", "клик": "click", "нажать": "click",
        "ввести": "type", "ввод": "type", "набрать": "type",
        "прокрутить": "scroll", "прокрутка": "scroll",
        "навести": "hover", "наведение": "hover",
        "закрыть": "close_modal", "закрыть модалку": "close_modal",
        "выбрать": "select_option", "выбрать опцию": "select_option",
        "клавиша": "press_key",
        "дефект": "check_defect", "баг": "check_defect",
        "исследовать": "explore", "обзор": "explore",
    }
    act = rus_map.get(act, act)
    if act not in VALID_ACTIONS:
        LOG.warning("validate_llm_action: неизвестное действие '%s', fallback на explore", act)
        act = "explore"
    action["action"] = act

    sel = (action.get("selector") or "").strip()
    val = (action.get("value") or "").strip()
    # selector обязателен для click, type, hover
    if act in ("click", "hover") and not sel:
        LOG.warning("validate_llm_action: пустой selector для %s", act)
    if act == "type" and (not sel or not val):
        LOG.warning("validate_llm_action: пустой selector или value для type")

    return action


def _build_system_prompt(
    phase_instruction: Optional[str] = None,
    tester_phase: Optional[str] = None,
    has_overlay: bool = False,
) -> str:
    """
    Динамический системный промпт: базовая роль + блоки по ситуации.
    Вместо одного огромного промпта — компактное ядро и контекстные блоки.
    """
    try:
        from config import DEMO_MODE
    except ImportError:
        DEMO_MODE = False

    if DEMO_MODE:
        base = """Ты — активный энергичный тестировщик. Действуй БЫСТРО и РЕШИТЕЛЬНО.

ПРАВИЛА:
1) Предпочитай КЛИКИ и ВВОД. Scroll — только если нечего кликнуть на экране.
2) Кликай на ВСЕ кнопки, ссылки, элементы меню. Заполняй ВСЕ формы.
3) Открывай дропдауны, выбирай опции, переключай табы, ходи по навигации.
4) Вводи реалистичные данные: test@test.com, Иван Тестов, +79991234567, Москва.
5) Не повторяй одно и то же — двигайся дальше. Каждый шаг = НОВЫЙ элемент.
6) Если видишь форму — заполни ВСЕ поля и нажми кнопку отправки.
7) Если видишь меню/навигацию — пройди по пунктам.
8) Баги (check_defect) — только при явных ошибках (500, сломанная верстка). Не придирайся.

СТРОГО JSON (без markdown):
{
  "action": "click|type|scroll|hover|close_modal|select_option|press_key|check_defect|explore",
  "selector": "текст кнопки, aria-label, id, data-testid",
  "value": "текст/опция/клавиша",
  "reason": "зачем (кратко)",
  "test_goal": "что проверяю",
  "expected_outcome": "что жду",
  "observation": "что вижу",
  "possible_bug": "баг или null",
  "layout_issue": "проблема верстки или null"
}

Приоритет: кнопки CTA → формы (заполнить+отправить) → навигация → табы → дропдауны → ссылки.
НЕ предлагай explore/scroll, пока есть элементы для click/type.
НЕ предлагай СТОП."""
    else:
        base = """Ты — опытный ручной тестировщик веб-приложений. Ты выполняешь ОДНО действие за шаг, проверяешь результат, затем решаешь следующий шаг.

Принципы:
1) Один шаг — одна цель: test_goal (что проверяю) и expected_outcome (что должно произойти).
2) Стабильные селекторы: текст кнопки, aria-label, id, data-testid, role. Не хрупкие CSS.
3) Не повторяй одно и то же. Если уже проверял элемент — переходи к другому.
4) Дефекты: только воспроизводимые баги приложения. Не 404, не флак, не сбой среды.
5) Служебный оверлей (Kventin, GigaChat, AI-тестировщик) — НЕ часть приложения. Игнорируй его.
6) Верстка: оценивай расположение (наложения, обрезки, сломанная сетка, кнопки вне экрана).

СТРОГО JSON (без markdown):
{
  "action": "click|type|scroll|hover|close_modal|select_option|press_key|check_defect|explore",
  "selector": "CSS-селектор, текст, aria-label, data-testid или id элемента",
  "value": "текст (type) / опция (select_option) / клавиша (press_key)",
  "reason": "зачем",
  "test_goal": "что проверяю",
  "expected_outcome": "что должно произойти",
  "observation": "что вижу (кратко)",
  "possible_bug": "описание бага или null",
  "layout_issue": "проблема верстки или null"
}

Приоритет элементов: CTA (главные кнопки) → формы → навигация → меню → футер → мелочи.
В формах — реалистичные тестовые данные (test@test.com, Иван Тестов, +79991234567).
НЕ предлагай СТОП."""

    blocks = []

    # Блок по фазе
    if phase_instruction:
        blocks.append(f"\n{phase_instruction}")
    if tester_phase:
        blocks.append(f"(текущая фаза: {tester_phase})")

    # Блок по оверлею
    if has_overlay:
        blocks.append("""
Модалки/оверлеи: сначала протестируй содержимое (кнопки, поля), потом закрой (close_modal).
Дропдауны: открыть → выбрать опцию → проверить. Тултипы: hover → проверить текст.""")

    return base + "\n".join(blocks)


def consult_agent_with_screenshot(
    context: str,
    question: str,
    screenshot_b64: Optional[str] = None,
    phase_instruction: Optional[str] = None,
    tester_phase: Optional[str] = None,
    has_overlay: bool = False,
) -> Optional[str]:
    """
    Задать GigaChat вопрос со скриншотом. Режим «реальный тестировщик».
    Retry при пустом ответе.
    """
    system = _build_system_prompt(phase_instruction, tester_phase, has_overlay)
    full_prompt = f"{context}\n\n{question}"
    return _llm_call_with_retry(full_prompt, screenshot_b64=screenshot_b64, system=system)


def get_test_plan_from_screenshot(screenshot_b64: Optional[str], url: str) -> List[str]:
    """
    По скриншоту главной страницы получить от GigaChat короткий тест-план (5–7 шагов).
    Возвращает список строк — шагов для тестирования.
    """
    system = "Ты — тест-аналитик. По скриншоту главной страницы составь краткий тест-план. Отвечай ТОЛЬКО нумерованным списком из 5–7 шагов на русском, по одному шагу на строку. Без вступления и выводов. Пример: 1. Кликнуть по меню. 2. Проверить форму поиска."
    prompt = f"URL: {url}\n\nСоставь тест-план из 5–7 конкретных шагов для тестирования этой страницы."
    raw = _get_client().chat_with_screenshot(prompt, screenshot_b64=screenshot_b64, system=system)
    if not raw:
        return []
    steps = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Убрать нумерацию "1. " или "1) "
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        if len(line) > 10:
            steps.append(line[:200])
    return steps[:10]


def ask_is_this_really_bug(bug_description: str, screenshot_b64: Optional[str]) -> bool:
    """
    Второй проход: GigaChat смотрит описание и скриншот и решает — это точно баг приложения?
    Возвращает True если да (создаём тикет), False если нет (пропускаем).
    """
    system = "Ты — ревьюер дефектов. Тебе прислали описание возможного бага и скриншот. Ответь СТРОГО одним словом: ДА — если это реальный баг приложения (не ожидаемое поведение, не проблема окружения, не флак). НЕТ — если это не баг (нормальное поведение, 404 в консоли, аналитика, тестовая среда)."
    prompt = f"Описание от тестировщика:\n{bug_description[:1500]}\n\nЭто точно баг приложения? Ответь одним словом: ДА или НЕТ."
    raw = _get_client().chat_with_screenshot(prompt, screenshot_b64=screenshot_b64, system=system)
    if not raw:
        return True  # при сбое LLM — создаём тикет
    low = raw.strip().lower()
    if "нет" in low or "не баг" in low or "не дефект" in low:
        return False
    return "да" in low
