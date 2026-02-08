"""
Клиент GigaChat API для консультаций агента.
Поддержка как в твоём проекте: token_header (готовый Bearer), свой gateway (api_url/token_url),
OAuth (authorization_key или client_id+client_secret), password grant (username, password, client_id).
"""
import base64
import os
import time
import uuid
from typing import Optional, List, Dict, Any

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# Публичный API (если не заданы свои URL)
DEFAULT_TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


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
            return None
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {basic_key}",
        }
        data = f"scope={self.scope}"
        try:
            r = requests.post(
                self.token_url,
                data=data,
                headers=headers,
                verify=self.verify_ssl,
                timeout=30,
            )
        except Exception as e:
            print(f"[GigaChat] Ошибка подключения OAuth: {e}")
            return None
        if r.status_code != 200:
            print(f"[GigaChat] Ошибка токена OAuth {r.status_code}: {r.text[:500]}")
            return None
        try:
            payload = r.json()
        except Exception:
            return None
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 1800) or 1800)
        if token:
            self.access_token = token
            self.token_expires_at = time.time() + expires_in
            return token
        return None

    def _get_token_password_grant(self) -> Optional[str]:
        if not (self.username and self.password and self.client_id):
            return None
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
            if r.status_code != 200:
                print(f"[GigaChat] Ошибка password-grant {r.status_code}: {r.text[:500]}")
                return None
            data = r.json()
            self.access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 1800) or 1800)
            self.token_expires_at = time.time() + expires_in
            return self.access_token
        except Exception as e:
            print(f"[GigaChat] Ошибка password-grant: {e}")
            return None

    def _get_token(self) -> Optional[str]:
        if self.token_header:
            s = self.token_header.strip()
            if s.lower().startswith("bearer "):
                return s[7:].strip()
            return s
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token
        token = self._get_token_oauth()
        if token:
            return token
        token = self._get_token_password_grant()
        return token

    def chat(self, messages: List[Dict[str, str]]) -> str:
        token = self._get_token()
        if not token:
            print("[GigaChat] Не удалось получить токен (нет token_header, authorization_key или password).")
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
        try:
            r = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                verify=self.verify_ssl,
                timeout=60,
            )
            if r.status_code != 200:
                print(f"[GigaChat] Ошибка API {r.status_code}: {r.text[:800]}")
                return ""
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            return (msg.get("content") or "").strip()
        except Exception as e:
            print(f"[GigaChat] Ошибка запроса: {e}")
            return ""

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        system = system or "Отвечай на русском. Кратко и по делу."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        return self.chat(messages)


# Глобальный клиент (ленивая инициализация)
_client: Optional[GigaChatClient] = None


def _get_client() -> GigaChatClient:
    global _client
    if _client is None:
        _client = GigaChatClient()
    return _client


def ask_gigachat(prompt: str, **kwargs: Any) -> Optional[str]:
    """Один запрос к GigaChat. Поддерживаются все способы авторизации из конфига."""
    result = _get_client().query(prompt, system=kwargs.get("system"))
    return result if result else None


def consult_agent(context: str, question: str) -> Optional[str]:
    """
    Задать GigaChat вопрос в контексте тестирования.
    context: сводка по консоли, сети, DOM, текущему URL.
    question: что спросить.
    """
    full_prompt = f"""Ты — помощник автотеста. Контекст страницы и наблюдения:
{context}

Вопрос: {question}

Отвечай кратко и по делу. Для выбора действия: укажи один конкретный совет (например: "Кликни по кнопке с текстом X" или "Создай дефект: описание"). Для дефектов: пиши только если это явный баг приложения, не флак и не проблема тестовой среды (не 404, не внешние скрипты)."""
    return ask_gigachat(full_prompt)
