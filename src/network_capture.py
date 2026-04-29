"""
Сбор полного network-лога браузерной сессии для прикрепления HAR к дефектам.

Зачем своё, а не Playwright `record_har_path`:
- HAR от Playwright пишется только при закрытии контекста; нам нужен снимок
  «на момент заведения дефекта» (любой шаг сессии, не финал);
- удобно фильтровать окно времени (последние N секунд / N запросов).

Использование:
    cap = NetworkCapture()
    cap.attach(page)              # или cap.attach_to_context(context, page)
    ...
    har = cap.build_har(page_url=page.url)            # все запросы
    har = cap.build_har(page_url=page.url, since_ts=t0)  # за окно

Формат — HAR 1.2 (минимально совместимый): открывается в Chrome DevTools и в
веб-просмотрщиках вроде https://toolbox.googleapps.com/apps/har_analyzer/.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("kventin.netcap")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_headers(obj: Any) -> List[Dict[str, str]]:
    """Преобразовать заголовки из Playwright в список HAR `{name, value}`."""
    out: List[Dict[str, str]] = []
    if obj is None:
        return out
    try:
        items = obj.items() if hasattr(obj, "items") else obj
        for k, v in items:
            out.append({"name": str(k)[:200], "value": str(v)[:2000]})
    except Exception:
        pass
    return out


class NetworkCapture:
    """Слабая привязка к Playwright: сами события приходят строго из main thread."""

    def __init__(self, max_entries: int = 2000) -> None:
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._by_request: Dict[int, Dict[str, Any]] = {}
        self._entries: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Подписка
    # ------------------------------------------------------------------

    def attach(self, page) -> None:
        """Подписаться на события одной страницы."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_failed)

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------

    def _on_request(self, request) -> None:
        try:
            entry: Dict[str, Any] = {
                "_started_ts": time.time(),
                "startedDateTime": _now_iso(),
                "time": -1,
                "request": {
                    "method": request.method,
                    "url": request.url,
                    "httpVersion": "HTTP/1.1",
                    "cookies": [],
                    "headers": _safe_headers(getattr(request, "headers", {})),
                    "queryString": [],
                    "headersSize": -1,
                    "bodySize": len(request.post_data or "") if request.post_data else 0,
                    "_resourceType": getattr(request, "resource_type", "") or "",
                },
                "response": {
                    "status": 0,
                    "statusText": "",
                    "httpVersion": "HTTP/1.1",
                    "cookies": [],
                    "headers": [],
                    "content": {"size": 0, "mimeType": ""},
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": -1,
                },
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
                "serverIPAddress": "",
                "_initiator": getattr(request, "frame", None).url if getattr(request, "frame", None) else "",
            }
            if request.post_data:
                entry["request"]["postData"] = {
                    "mimeType": "",
                    "text": (request.post_data or "")[:8000],
                }
            with self._lock:
                self._by_request[id(request)] = entry
        except Exception:
            LOG.debug("on_request: ошибка", exc_info=True)

    def _on_response(self, response) -> None:
        try:
            req = response.request
            with self._lock:
                entry = self._by_request.get(id(req))
            if not entry:
                return
            entry["response"]["status"] = response.status
            entry["response"]["statusText"] = getattr(response, "status_text", "") or ""
            entry["response"]["headers"] = _safe_headers(getattr(response, "headers", {}))
            mime = ""
            for h in entry["response"]["headers"]:
                if h["name"].lower() == "content-type":
                    mime = h["value"]
                    break
            entry["response"]["content"]["mimeType"] = mime
        except Exception:
            LOG.debug("on_response: ошибка", exc_info=True)

    def _on_request_finished(self, request) -> None:
        try:
            with self._lock:
                entry = self._by_request.pop(id(request), None)
            if not entry:
                return
            entry["time"] = max(0, int((time.time() - entry["_started_ts"]) * 1000))
            entry["timings"]["wait"] = entry["time"]
            try:
                response = request.response()
                if response is not None:
                    sizes = response.request.sizes() if hasattr(response.request, "sizes") else None
                    if sizes:
                        entry["request"]["bodySize"] = sizes.get("requestBodySize", -1)
                        entry["response"]["bodySize"] = sizes.get("responseBodySize", -1)
            except Exception:
                pass
            self._append_entry(entry)
        except Exception:
            LOG.debug("on_request_finished: ошибка", exc_info=True)

    def _on_request_failed(self, request) -> None:
        try:
            with self._lock:
                entry = self._by_request.pop(id(request), None)
            if not entry:
                return
            entry["time"] = max(0, int((time.time() - entry["_started_ts"]) * 1000))
            entry["response"]["status"] = 0
            entry["response"]["statusText"] = (request.failure or "request_failed")[:200]
            self._append_entry(entry)
        except Exception:
            LOG.debug("on_request_failed: ошибка", exc_info=True)

    def _append_entry(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                del self._entries[: len(self._entries) - self.max_entries]

    # ------------------------------------------------------------------
    # Снапшоты
    # ------------------------------------------------------------------

    def total(self) -> int:
        with self._lock:
            return len(self._entries)

    def build_har(
        self,
        *,
        page_url: str = "",
        since_ts: Optional[float] = None,
        last_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Собрать HAR (dict) из накопленных запросов. Без побочных эффектов.

        since_ts: unix-time, оставить только запросы стартовавшие позже.
        last_n:   взять только последние N (после фильтра по времени).
        """
        with self._lock:
            entries = list(self._entries)
        if since_ts is not None:
            entries = [e for e in entries if e.get("_started_ts", 0) >= since_ts]
        if last_n is not None and last_n > 0:
            entries = entries[-last_n:]
        for e in entries:
            e.pop("_started_ts", None)
            e.pop("_initiator", None)
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "kventin", "version": "1.0"},
                "pages": [{
                    "startedDateTime": _now_iso(),
                    "id": "page_1",
                    "title": page_url or "",
                    "pageTimings": {},
                }],
                "entries": entries,
            }
        }
        return har

    def dump_har_to(
        self,
        path: str,
        *,
        page_url: str = "",
        since_ts: Optional[float] = None,
        last_n: Optional[int] = None,
    ) -> bool:
        """Записать HAR в файл. True/False — успех."""
        try:
            data = self.build_har(page_url=page_url, since_ts=since_ts, last_n=last_n)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            LOG.warning("dump_har: не удалось записать %s", path, exc_info=True)
            return False


__all__ = ["NetworkCapture"]
