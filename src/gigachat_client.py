"""
Клиент GigaChat API для консультаций агента при принятии решений.
Поддерживает авторизацию по credentials или client_id/client_secret.
"""
import json
import os
import time
from typing import Optional

import requests

# Базовый URL API GigaChat (официальный)
GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


def get_access_token(credentials: Optional[str] = None, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Optional[str]:
    """Получить access_token для GigaChat API."""
    credentials = credentials or os.getenv("GIGACHAT_CREDENTIALS")
    client_id = client_id or os.getenv("GIGACHAT_CLIENT_ID")
    client_secret = client_secret or os.getenv("GIGACHAT_CLIENT_SECRET")

    if credentials:
        # Авторизация по Base64 credentials (client_id:client_secret)
        import base64
        auth = base64.b64encode(credentials.encode() if isinstance(credentials, str) else credentials).decode()
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"scope": "GIGACHAT_API_PERS"}
    elif client_id and client_secret:
        auth = __import__("base64").b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"scope": "GIGACHAT_API_PERS"}
    else:
        return None

    try:
        r = requests.post(GIGACHAT_AUTH_URL, headers=headers, data=data, verify=False, timeout=30)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        print(f"[GigaChat] Ошибка авторизации: {e}")
        return None


def ask_gigachat(
    prompt: str,
    *,
    credentials: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    access_token: Optional[str] = None,
    model: str = "GigaChat",
) -> Optional[str]:
    """
    Отправить запрос в GigaChat и получить ответ.
    Используется агентом для принятия решений (что кликать, создавать ли дефект и т.д.).
    """
    token = access_token or get_access_token(credentials=credentials, client_id=client_id, client_secret=client_secret)
    if not token:
        return None

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    try:
        r = requests.post(
            GIGACHAT_CHAT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            verify=False,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        msg = choice.get("message") or {}
        return (msg.get("content") or "").strip()
    except Exception as e:
        print(f"[GigaChat] Ошибка запроса: {e}")
        return None


def consult_agent(context: str, question: str) -> Optional[str]:
    """
    Задать GigaChat вопрос в контексте тестирования.
    context: сводка по консоли, сети, DOM, текущему URL.
    question: что спросить (например: "Какой элемент лучше кликнуть следующим?" или "Является ли это дефектом для Jira?").
    """
    full_prompt = f"""Ты — помощник автотеста. Контекст страницы и наблюдения:
{context}

Вопрос: {question}

Отвечай кратко и по делу. Для выбора действия: укажи один конкретный совет (например: "Кликни по кнопке с текстом X" или "Создай дефект: описание"). Для дефектов: пиши только если это явный баг приложения, не флак и не проблема тестовой среды (не 404, не внешние скрипты)."""
    return ask_gigachat(full_prompt)
