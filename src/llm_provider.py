"""
Абстракция провайдеров LLM: GigaChat, Jan, OpenAI, Anthropic, Ollama.
Выбор по LLM_PROVIDER и соответствующим переменным окружения.
"""
import base64
import logging
import os
from typing import Optional, List, Dict, Any

LOG = logging.getLogger("LLM")


def get_llm_client():
    """
    Вернуть клиент LLM по конфигу LLM_PROVIDER.
    Клиент имеет методы: query(prompt, system), chat_with_screenshot(prompt, screenshot_b64, system),
    и опционально _get_token() для инициализации (GigaChat).
    """
    try:
        from config import LLM_PROVIDER
    except ImportError:
        LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()

    if LLM_PROVIDER == "jan":
        from src.jan_client import JanClient
        LOG.info("Using LLM: Jan")
        return JanClient()

    if LLM_PROVIDER == "openai":
        LOG.info("Using LLM: OpenAI")
        return _OpenAIClient()

    if LLM_PROVIDER == "anthropic":
        LOG.info("Using LLM: Anthropic")
        return _AnthropicClient()

    if LLM_PROVIDER == "ollama":
        LOG.info("Using LLM: Ollama")
        return _OllamaClient()

    # gigachat по умолчанию — возвращаем None, чтобы gigachat_client подставил GigaChatClient (избегаем циклического импорта)
    return None


def _compress_screenshot_b64(screenshot_b64: str, max_width: int = 1280) -> bytes:
    """Сжать PNG base64 в JPEG bytes."""
    raw = base64.b64decode(screenshot_b64)
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


class _OpenAIClient:
    """OpenAI API (gpt-4o, gpt-4o-mini с vision)."""

    def __init__(self):
        try:
            from config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL
        except ImportError:
            OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
            OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = OPENAI_API_KEY
        self.model = OPENAI_MODEL or "gpt-4o-mini"
        self.base_url = OPENAI_BASE_URL or "https://api.openai.com/v1"
        self.chat_url = f"{self.base_url}/chat/completions"

    def _get_token(self):
        return self.api_key or None

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        messages = [{"role": "system", "content": system or "Отвечай на русском. Кратко."}, {"role": "user", "content": prompt}]
        return self._chat(messages)

    def chat_with_screenshot(self, text_prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> str:
        system = system or "Ты — AI-тестировщик. Отвечай на русском."
        content = [{"type": "text", "text": text_prompt}]
        if screenshot_b64:
            try:
                jpeg_bytes = _compress_screenshot_b64(screenshot_b64)
                b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            except Exception:
                pass
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        return self._chat(messages)

    def _chat(self, messages: List[Dict]) -> str:
        import requests
        if not self.api_key:
            LOG.error("OPENAI_API_KEY не задан")
            return ""
        try:
            r = requests.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": messages, "max_tokens": 2048, "temperature": 0.2},
                timeout=120,
            )
            if r.status_code != 200:
                LOG.error("OpenAI %s %s", r.status_code, r.text[:500])
                return ""
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            return (choices[0].get("message") or {}).get("content") or ""
        except Exception as e:
            LOG.exception("OpenAI request: %s", e)
            return ""


class _AnthropicClient:
    """Anthropic API (Claude с vision)."""

    def __init__(self):
        try:
            from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
        except ImportError:
            ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
            ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        self.api_key = ANTHROPIC_API_KEY
        self.model = ANTHROPIC_MODEL or "claude-sonnet-4-20250514"

    def _get_token(self):
        return self.api_key or None

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        return self._chat([{"type": "text", "text": prompt}], system)

    def chat_with_screenshot(self, text_prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> str:
        system = system or "Ты — AI-тестировщик. Отвечай на русском."
        content = [{"type": "text", "text": text_prompt}]
        if screenshot_b64:
            try:
                jpeg_bytes = _compress_screenshot_b64(screenshot_b64)
                b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            except Exception:
                pass
        return self._chat(content, system)

    def _chat(self, content: List[Dict], system: Optional[str] = None) -> str:
        try:
            import anthropic
            c = anthropic.Anthropic(api_key=self.api_key)
            sys = system or "Отвечай на русском. Кратко."
            msg = c.messages.create(
                model=self.model,
                max_tokens=2048,
                system=sys,
                messages=[{"role": "user", "content": content}],
            )
            if msg.content and len(msg.content) > 0:
                block = msg.content[0]
                if hasattr(block, "text"):
                    return block.text
            return ""
        except ImportError:
            LOG.error("anthropic package not installed: pip install anthropic")
            return ""
        except Exception as e:
            LOG.exception("Anthropic request: %s", e)
            return ""


class _OllamaClient:
    """Ollama local (llava, llama3.2-vision и др.)."""

    def __init__(self):
        try:
            from config import OLLAMA_HOST, OLLAMA_MODEL
        except ImportError:
            OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
            OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llava")
        self.base_url = OLLAMA_HOST or "http://127.0.0.1:11434"
        self.model = OLLAMA_MODEL or "llava"
        self.chat_url = f"{self.base_url}/api/chat"

    def _get_token(self):
        return "ok"

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._request(messages)

    def chat_with_screenshot(self, text_prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> str:
        if not screenshot_b64:
            return self.query(text_prompt, system)
        try:
            import requests
            images = [screenshot_b64]
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": text_prompt, "images": images}],
                "stream": False,
            }
            r = requests.post(self.chat_url, json=payload, timeout=120)
            if r.status_code != 200:
                LOG.error("Ollama %s %s", r.status_code, r.text[:300])
                return self.query(text_prompt, system)
            data = r.json()
            msg = data.get("message") or {}
            return (msg.get("content") or "").strip()
        except Exception as e:
            LOG.exception("Ollama request: %s", e)
            return self.query(text_prompt, system)

    def _request(self, messages) -> str:
        try:
            import requests
            payload = {"model": self.model, "messages": messages, "stream": False}
            r = requests.post(self.chat_url, json=payload, timeout=120)
            if r.status_code != 200:
                return ""
            data = r.json()
            return (data.get("message") or {}).get("content") or ""
        except Exception:
            return ""
