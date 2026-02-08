"""
Клиент для локальной модели в Jan (OpenAI-совместимый API).
Подходит для Mac M4 32GB: Vikhr-7B (русский), Qwen2.5-7B, Llama 3.2 и др.
"""
import base64
import logging
from typing import Optional, List, Dict, Any

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

LOG = logging.getLogger("Jan")
LOG.setLevel(logging.DEBUG)
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[Jan] %(levelname)s %(message)s"))
    LOG.addHandler(h)


class JanClient:
    """
    Клиент к Jan Local API Server (http://127.0.0.1:1337 по умолчанию).
    OpenAI-совместимый: /v1/chat/completions.
    """

    def __init__(self):
        from config import JAN_API_URL, JAN_API_KEY, JAN_MODEL
        self.api_url = JAN_API_URL.rstrip("/")
        self.chat_url = f"{self.api_url}/v1/chat/completions"
        self.api_key = JAN_API_KEY or "jan-api-key"
        self.model = JAN_MODEL or "vikhr-7b-instruct"
        LOG.info("Jan client: %s, model=%s", self.chat_url, self.model)

    def _request(self, payload: Dict[str, Any], timeout: int = 120) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        LOG.debug("Jan POST %s model=%s", self.chat_url, payload.get("model"))
        try:
            r = requests.post(
                self.chat_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            LOG.info("Jan response %s len=%s", r.status_code, len(r.text))
            if r.status_code != 200:
                LOG.error("Jan error %s %s", r.status_code, r.text[:500])
                return ""
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                LOG.warning("Jan: no choices in response")
                return ""
            content = (choices[0].get("message") or {}).get("content") or ""
            return content.strip()
        except requests.exceptions.ConnectionError as e:
            LOG.error("Jan: connection error — запущен ли Jan? %s", e)
            return ""
        except Exception as e:
            LOG.exception("Jan request error: %s", e)
            return ""

    def chat(self, messages: List[Dict[str, Any]]) -> str:
        """Отправить сообщения в формате OpenAI (role + content). content может быть строкой или массивом (text + image_url)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 2048,
        }
        return self._request(payload)

    def chat_with_screenshot(
        self,
        text_prompt: str,
        screenshot_b64: Optional[str] = None,
        system: Optional[str] = None,
    ) -> str:
        """
        Промпт со скриншотом. Если модель с vision — отправляем image.
        Иначе — только текст (локальные текстовые модели типа Vikhr).
        """
        system = system or "Ты — AI-тестировщик. Отвечай на русском."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text_prompt},
        ]
        if screenshot_b64:
            user_content = [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]
            messages[-1]["content"] = user_content
        out = self.chat(messages)
        if not out and screenshot_b64:
            LOG.warning("Jan: ответ с картинкой пустой, пробуем без скриншота (текстовая модель)")
            messages[-1]["content"] = text_prompt + "\n\n[Скриншот страницы недоступен для этой модели — ориентируйся по тексту контекста выше.]"
            out = self.chat(messages)
        return out

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        system = system or "Отвечай на русском. Кратко."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        return self.chat(messages)
