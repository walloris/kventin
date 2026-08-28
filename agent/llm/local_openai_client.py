"""Local OpenAI-compatible LLM client.

The expected local server exposes:
- POST /v1/chat/completions
- GET  /v1/models
"""
from __future__ import annotations

import base64
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import requests

from agent.core.resilience import CircuitBreaker, RetryPolicy, parse_retry_after

LOG = logging.getLogger("LocalLLM")
RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


TLS_PROXY_HINT = (
    "Локальный OpenAI-compatible endpoint ответил ошибкой TLS/self-signed certificate. "
    "Это обычно падает не в Kventin, а внутри Node-прокси при запросе к upstream. "
    "Запустите прокси с доверенным корпоративным CA: "
    "NODE_EXTRA_CA_CERTS=/path/to/corporate-ca.pem node proxy.js. "
    "Временный небезопасный обход: NODE_TLS_REJECT_UNAUTHORIZED=0 node proxy.js."
)


def _looks_like_tls_proxy_error(text: str) -> bool:
    low = (text or "").lower()
    return any(
        marker in low
        for marker in (
            "self_signed_cert_in_chain",
            "self-signed certificate",
            "unable_to_verify_leaf_signature",
            "certificate verify failed",
            "fetch failed",
        )
    )


class LocalOpenAIClient:
    """Small client for a local OpenAI-compatible chat completions endpoint."""

    def __init__(self) -> None:
        try:
            from config import (
                LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS,
                LLM_CIRCUIT_BREAKER_COOLDOWN_SEC,
                LOCAL_LLM_API_KEY,
                LOCAL_LLM_API_URL,
                LOCAL_LLM_MODEL,
                LLM_REQUEST_TIMEOUT_SEC,
            )
        except ImportError:
            LOCAL_LLM_API_URL = os.getenv("LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1").rstrip("/")
            LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")
            LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "").strip()
            LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60"))
            LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS = int(
                os.getenv("LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS", "2")
            )
            LLM_CIRCUIT_BREAKER_COOLDOWN_SEC = int(
                os.getenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SEC", "60")
            )

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
        self._next_model_lookup_at = 0.0
        self._model_lookup_interval = max(
            10.0,
            float(LLM_CIRCUIT_BREAKER_COOLDOWN_SEC or 0),
        )
        threshold = int(LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS or 0)
        self.circuit = (
            CircuitBreaker(
                failure_threshold=threshold,
                cooldown_seconds=float(LLM_CIRCUIT_BREAKER_COOLDOWN_SEC or 0),
            )
            if threshold > 0
            else None
        )

    def _retry_policy(self) -> RetryPolicy:
        try:
            from config import LLM_RETRY_BASE_DELAY, LLM_RETRY_COUNT, LLM_RETRY_MAX_DELAY
        except ImportError:
            LLM_RETRY_COUNT = int(os.getenv("LLM_RETRY_COUNT", "3"))
            LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))
            LLM_RETRY_MAX_DELAY = float(os.getenv("LLM_RETRY_MAX_DELAY", "20.0"))
        return RetryPolicy(
            max_attempts=max(1, int(LLM_RETRY_COUNT)),
            base_delay=max(0.0, float(LLM_RETRY_BASE_DELAY)),
            max_delay=max(0.0, float(LLM_RETRY_MAX_DELAY)),
            retryable_statuses=RETRYABLE_HTTP_STATUSES,
        )

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> Optional[requests.Response]:
        policy = self._retry_policy()
        requester = requests.get if method.upper() == "GET" else requests.post
        last_error: Optional[BaseException] = None
        for attempt in range(policy.attempts()):
            try:
                response = requester(url, **kwargs)
                last_error = None
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                if attempt >= policy.attempts() - 1:
                    break
                delay = policy.delay_for(attempt, random_fn=random.uniform)
                LOG.warning(
                    "Local LLM transport error, retry %d/%d in %.1fs: %s",
                    attempt + 1,
                    policy.attempts(),
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue
            except requests.exceptions.RequestException as exc:
                last_error = exc
                break

            if response.status_code not in policy.retryable_statuses:
                return response
            if attempt >= policy.attempts() - 1:
                return response
            delay = policy.delay_for(
                attempt,
                retry_after=parse_retry_after(response.headers),
                random_fn=random.uniform,
            )
            LOG.warning(
                "Local LLM HTTP %s, retry %d/%d in %.1fs: %s",
                response.status_code,
                attempt + 1,
                policy.attempts(),
                delay,
                (response.text or "")[:500],
            )
            time.sleep(delay)

        if last_error is not None:
            LOG.warning("Local LLM is not reachable at %s: %s", url, last_error)
        return None

    def _get_token(self) -> str:
        """Compatibility hook used by the previous LLM init path."""
        return "local-openai-compatible"

    def _model(self) -> str:
        if self.model:
            return self.model
        now = time.monotonic()
        if now < self._next_model_lookup_at:
            return "local-model"
        self._next_model_lookup_at = now + self._model_lookup_interval
        try:
            response = self._request_with_retry(
                "GET",
                self.models_url,
                headers=self._headers(),
                timeout=min(self.timeout, 10),
            )
            data = response.json() if response is not None and response.status_code == 200 else {}
            models = data.get("data") or []
            first = models[0] if models else {}
            model_id = first.get("id") if isinstance(first, dict) else None
            if model_id:
                self.model = str(model_id)
                self._next_model_lookup_at = 0.0
                LOG.info("Selected local LLM model from /models: %s", self.model)
                return self.model
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not read local LLM models from %s: %s", self.models_url, exc)
        # Retry discovery after a cooldown instead of multiplying /models
        # requests for every prompt while the endpoint is unhealthy.
        return "local-model"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def healthcheck(self, timeout: float = 3.0) -> bool:
        """One cheap startup probe; normal requests own the retry budget."""
        try:
            response = requests.get(
                self.models_url,
                headers=self._headers(),
                timeout=max(0.2, min(float(timeout), 3.0)),
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def chat(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> str:
        if self.circuit is not None and not self.circuit.allow_request():
            state = self.circuit.snapshot()
            LOG.warning(
                "Local LLM circuit is open, fallback to deterministic policy for %.1fs",
                state["retry_in_seconds"],
            )
            return ""
        payload: Dict[str, Any] = {
            "model": self._model(),
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        response = self._request_with_retry(
            "POST",
            self.chat_url,
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response is None:
            if self.circuit is not None:
                self.circuit.record_failure()
            return ""
        if response.status_code != 200:
            body = response.text[:1200]
            if _looks_like_tls_proxy_error(body):
                LOG.error("%s\nProxy response: %s", TLS_PROXY_HINT, body)
            else:
                LOG.error("Local LLM HTTP %s: %s", response.status_code, body)
            if self.circuit is not None:
                self.circuit.record_failure()
            return ""
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            LOG.warning("Local LLM returned invalid JSON: %s", exc)
            if self.circuit is not None:
                self.circuit.record_failure()
            return ""
        choices = data.get("choices") or []
        if not choices:
            LOG.warning("Local LLM response has no choices: %s", str(data)[:500])
            if self.circuit is not None:
                self.circuit.record_failure()
            return ""
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        if content:
            if self.circuit is not None:
                self.circuit.record_success()
            return content
        if self.circuit is not None:
            self.circuit.record_failure()
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
