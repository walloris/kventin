"""Local OpenAI-compatible LLM client.

The expected local server exposes:
- POST /v1/chat/completions
- GET  /v1/models
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger("LocalLLM")


class LocalOpenAIClient:
    """Small client for a local OpenAI-compatible chat completions endpoint."""

    def __init__(self) -> None:
        try:
            from config import LOCAL_LLM_API_URL, LOCAL_LLM_API_KEY, LOCAL_LLM_MODEL, LLM_REQUEST_TIMEOUT_SEC
        except ImportError:
            LOCAL_LLM_API_URL = os.getenv("LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1").rstrip("/")
            LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")
            LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "").strip()
            LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60"))

        base_url = (LOCAL_LLM_API_URL or "http://127.0.0.1:3333/v1").rstrip("/")
        if base_url.endswith("/chat/completions"):
            self.chat_url = base_url
            self.base_url = base_url[: -len("/chat/completions")]
        else:
            self.base_url = base_url
            self.chat_url = f"{self.base_url}/chat/completions"
        self.models_url = f"{self.base_url}/models"
        self.api_key = LOCAL_LLM_API_KEY or "local"
        self.model = (LOCAL_LLM_MODEL or "").strip()
        self.timeout = max(5, int(LLM_REQUEST_TIMEOUT_SEC))

    def _get_token(self) -> str:
        """Compatibility hook used by the previous LLM init path."""
        return "local-openai-compatible"

    def _model(self) -> str:
        if self.model:
            return self.model
        try:
            data = requests.get(self.models_url, headers=self._headers(), timeout=min(self.timeout, 10)).json()
            models = data.get("data") or []
            first = models[0] if models else {}
            model_id = first.get("id") if isinstance(first, dict) else None
            if model_id:
                self.model = str(model_id)
                LOG.info("Selected local LLM model from /models: %s", self.model)
                return self.model
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not read local LLM models from %s: %s", self.models_url, exc)
        self.model = "local-model"
        return self.model

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def chat(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self._model(),
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            response = requests.post(
                self.chat_url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if response.status_code != 200:
                LOG.error("Local LLM HTTP %s: %s", response.status_code, response.text[:800])
                return ""
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                LOG.warning("Local LLM response has no choices: %s", str(data)[:500])
                return ""
            return ((choices[0].get("message") or {}).get("content") or "").strip()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            LOG.warning("Local LLM is not reachable at %s: %s", self.chat_url, exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Local LLM request failed: %s", exc)
            return ""

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        messages = [
            {"role": "system", "content": system or "Отвечай на русском. Кратко и по делу."},
            {"role": "user", "content": prompt},
        ]
        return self.chat(messages)

    def chat_with_screenshot(
        self,
        text_prompt: str,
        screenshot_b64: Optional[str] = None,
        system: Optional[str] = None,
    ) -> str:
        content: Any = text_prompt
        if screenshot_b64:
            b64 = self._to_jpeg_b64(screenshot_b64)
            content = [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        messages = [
            {"role": "system", "content": system or "Ты — AI-тестировщик. Отвечай на русском."},
            {"role": "user", "content": content},
        ]
        return self.chat(messages)

    def _to_jpeg_b64(self, screenshot_b64: str) -> str:
        try:
            from io import BytesIO
            from PIL import Image

            raw = base64.b64decode(screenshot_b64)
            image = Image.open(BytesIO(raw))
            if image.width > 1280:
                ratio = 1280 / image.width
                image = image.resize((1280, int(image.height * ratio)), Image.LANCZOS)
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            out = BytesIO()
            image.save(out, format="JPEG", quality=75, optimize=True)
            return base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:
            return screenshot_b64
