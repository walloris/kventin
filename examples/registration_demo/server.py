#!/usr/bin/env python3
"""Local registration app and a tiny Jira-compatible debug tracker."""

import argparse
import html as html_module
import json
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


STATIC_DIR = Path(__file__).resolve().parent
BUGGY_RELEASE = "1.0.0-buggy"
FIXED_RELEASE = "1.0.1-fixed"


def _jira_field_name(value: Any, fallback: str = "-") -> str:
    if isinstance(value, dict):
        for key in ("name", "value", "key", "id"):
            if value.get(key):
                return str(value[key])
        return fallback
    return str(value) if value not in (None, "") else fallback


def _split_wiki_description(description: str) -> List[Tuple[str, str]]:
    sections: List[Tuple[str, str]] = []
    title = "Описание"
    body: List[str] = []
    for raw_line in (description or "").splitlines():
        heading = re.match(r"^h[34]\.\s+(.+?)\s*$", raw_line)
        if heading:
            if body or title != "Описание":
                sections.append((title, "\n".join(body).strip()))
            title = heading.group(1).strip()
            body = []
            continue
        body.append(raw_line)
    if body or title != "Описание":
        sections.append((title, "\n".join(body).strip()))
    return sections


def _clean_wiki_text(value: str) -> str:
    cleaned = re.sub(r"\{(?:quote|code)(?::[^}]*)?\}", "", value or "")
    cleaned = re.sub(r"(?m)^h[1-6]\.\s+", "", cleaned)
    cleaned = cleaned.replace("{{", "").replace("}}", "")
    return cleaned.strip()


def _render_description(description: str) -> str:
    rendered: List[str] = []
    for title, body in _split_wiki_description(description):
        if not body:
            continue
        safe_title = html_module.escape(title)
        safe_body = html_module.escape(_clean_wiki_text(body))
        section_class = "description-section"
        if "Шаги воспроизведения" in title:
            section_class += " reproduction-steps"
        if "Сценарий автоматического ретеста" in title:
            rendered.append(
                "<details class='retest-plan' data-section='%s'>"
                "<summary>%s</summary><pre>%s</pre></details>" % (
                    html_module.escape(title, quote=True),
                    safe_title,
                    safe_body,
                )
            )
            continue
        rendered.append(
            "<section class='%s' data-section='%s'><h3>%s</h3><pre>%s</pre></section>" % (
                section_class,
                html_module.escape(title, quote=True),
                safe_title,
                safe_body,
            )
        )
    return "".join(rendered) or "<p class='empty-description'>Описание отсутствует</p>"


def _render_issue_card(issue: Dict[str, Any]) -> str:
    status = html_module.escape(str(issue.get("status") or "Open"))
    status_class = re.sub(r"[^a-z]+", "-", status.casefold()).strip("-")
    comments = issue.get("comments") or []
    latest_comment = (
        html_module.escape(_clean_wiki_text(str(comments[-1])))
        if comments else "Комментариев пока нет"
    )
    labels = issue.get("labels") or []
    labels_html = " ".join(
        "<span class='label'>%s</span>" % html_module.escape(str(label))
        for label in labels
    ) or "-"
    attributes = (
        ("Тип", issue.get("issue_type") or "Bug"),
        ("Приоритет", issue.get("priority") or "-"),
        ("Исполнитель", issue.get("assignee") or "Unassigned"),
        ("Автор", issue.get("reporter") or "Kventin Agent"),
        ("Release", issue.get("release") or "-"),
        ("Evidence", "%s файла" % len(issue.get("attachments") or [])),
    )
    attributes_html = "".join(
        "<div class='attribute'><dt>%s</dt><dd>%s</dd></div>" % (
            html_module.escape(str(label)),
            html_module.escape(str(value)),
        )
        for label, value in attributes
    )
    resolution = html_module.escape(str(issue.get("resolution") or "-"))
    return (
        "<article class='issue'>"
        "<header class='issue-head'><div><strong>%s</strong><h2>%s</h2></div>"
        "<span class='status status-%s'>%s</span></header>"
        "<div class='issue-layout'><main class='issue-description'>"
        "<h3 class='column-title'>Описание дефекта</h3>%s</main>"
        "<aside class='issue-attributes'><h3 class='column-title'>Атрибуты</h3>"
        "<dl>%s<div class='attribute'><dt>Resolution</dt><dd>%s</dd></div>"
        "<div class='attribute labels'><dt>Labels</dt><dd>%s</dd></div></dl>"
        "<section class='attachments'><h3>Вложения</h3><p>%s</p></section>"
        "<section class='latest-comment'><h3>Последний комментарий</h3><pre>%s</pre></section>"
        "</aside></div></article>" % (
            html_module.escape(str(issue.get("key") or "-")),
            html_module.escape(str(issue.get("summary") or "Без заголовка")),
            status_class,
            status,
            _render_description(str(issue.get("description") or "")),
            attributes_html,
            resolution,
            labels_html,
            ", ".join(html_module.escape(str(name)) for name in issue.get("attachments") or []) or "Нет",
            latest_comment,
        )
    )


_DEBUG_JIRA_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Debug Jira</title><style>
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f5f7fa;color:#17202a;font:15px Inter,system-ui,sans-serif}
.page{width:min(1180px,calc(100% - 48px));margin:0 auto;padding:54px 0 80px}.top{display:flex;align-items:end;justify-content:space-between;margin-bottom:24px}
.eyebrow{margin:0 0 8px;color:#087f8c;font-weight:800}.top h1{margin:0;font-size:38px;letter-spacing:0}.release{border:1px solid #cbd5df;border-radius:6px;background:white;padding:10px 14px;font-weight:700}
.issues{display:grid;gap:18px}.issue{border:1px solid #d7e0e8;border-radius:8px;background:white;overflow:hidden}.issue-head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding:22px 24px;border-bottom:1px solid #e5eaf0}
.issue-head strong{color:#087f8c}.issue-head h2{margin:9px 0 0;font-size:23px}.status{flex:none;border-radius:999px;background:#eef2f6;color:#344150;padding:6px 10px;font-size:13px;font-weight:800}
.status-open{background:#fff4e5;color:#9a5b00}.status-ready-for-qa,.status-qa{background:#e8f1ff;color:#175cd3}.status-closed{background:#e8f8ef;color:#067647}
.issue-layout{display:grid;grid-template-columns:minmax(0,1fr) 310px}.issue-description{padding:24px 28px 36px}.issue-attributes{border-left:1px solid #e5eaf0;background:#fbfcfd;padding:24px}
.column-title{margin:0 0 18px;font-size:13px;color:#667485;text-transform:uppercase}.description-section{padding:0 0 22px;margin:0 0 22px;border-bottom:1px solid #edf0f3}.description-section h3{margin:0 0 10px;font-size:17px}
.description-section pre,.latest-comment pre,.retest-plan pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 Inter,system-ui,sans-serif;color:#44515f}.reproduction-steps{border:1px solid #b9dfe3;border-left:4px solid #087f8c;border-radius:6px;background:#f4fbfb;padding:18px}
.retest-plan{border:1px solid #d7e0e8;border-radius:6px;padding:13px;margin-top:18px}.retest-plan summary{cursor:pointer;font-weight:750;color:#087f8c}.retest-plan pre{margin-top:14px;font-family:ui-monospace,monospace;font-size:12px}
.issue-attributes dl{display:grid;grid-template-columns:1fr 1fr;gap:16px 12px;margin:0}.attribute{display:grid;gap:4px}.attribute dt{color:#667485;font-size:11px;text-transform:uppercase}.attribute dd{margin:0;font-weight:700;overflow-wrap:anywhere}.attribute.labels{grid-column:1/-1}.label{display:inline-block;margin:0 4px 4px 0;border-radius:999px;background:#e8f1ff;color:#175cd3;padding:4px 8px;font-size:11px}
.attachments,.latest-comment{margin-top:24px;padding-top:20px;border-top:1px solid #e5eaf0}.attachments h3,.latest-comment h3{margin:0 0 9px;font-size:12px;text-transform:uppercase;color:#667485}.attachments p{margin:0;overflow-wrap:anywhere;color:#44515f}.latest-comment pre{font-size:12px}.empty{border:1px dashed #aeb9c5;border-radius:8px;padding:40px;text-align:center}
@media(max-width:820px){.page{width:min(100% - 24px,680px);padding-top:28px}.top{align-items:flex-start;gap:16px}.issue-layout{grid-template-columns:1fr}.issue-attributes{border-left:0;border-top:1px solid #e5eaf0}.issue-head{padding:18px}.issue-description{padding:20px}.top h1{font-size:30px}}
</style><div class="page"><header class="top"><div><p class="eyebrow">Kventin local tracker</p><h1>Debug Jira</h1></div>
<div class="release">Release: __DEBUG_RELEASE__</div></header><section class="issues">__DEBUG_CARDS__</section></div></html>"""


def render_debug_issues_page(snapshot: Dict[str, Any]) -> str:
    cards = "".join(_render_issue_card(issue) for issue in snapshot.get("issues") or [])
    if not cards:
        cards = "<p class='empty'>Дефектов пока нет</p>"
    return (
        _DEBUG_JIRA_PAGE
        .replace("__DEBUG_RELEASE__", html_module.escape(str(snapshot.get("release") or "-")))
        .replace("__DEBUG_CARDS__", cards)
    )


class DemoState:
    """Thread-safe application release and debug Jira state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.release = BUGGY_RELEASE
        self._issues: Dict[str, Dict[str, Any]] = {}
        self._next_issue = 1

    def register(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        required = ("fullName", "email", "password")
        missing = [name for name in required if not str(payload.get(name) or "").strip()]
        if missing or not payload.get("terms"):
            return 422, {"message": "Заполните обязательные поля и примите условия."}
        if "@" not in str(payload.get("email") or ""):
            return 422, {"message": "Укажите корректный email."}
        if len(str(payload.get("password") or "")) < 8:
            return 422, {"message": "Пароль должен содержать не менее 8 символов."}

        with self._lock:
            release = self.release
        if release == BUGGY_RELEASE:
            # Known defect REG-DEMO-1: valid registration crashes in the API.
            return 500, {
                "code": "PROFILE_INITIALIZATION_FAILED",
                "message": "Не удалось создать аккаунт.",
            }
        return 201, {
            "id": "demo-user-1",
            "email": str(payload.get("email")),
            "release": release,
        }

    def deploy_fix(self) -> None:
        with self._lock:
            self.release = FIXED_RELEASE

    def create_issue(self, fields: Dict[str, Any]) -> str:
        with self._lock:
            key = "DEMO-%d" % self._next_issue
            self._next_issue += 1
            self._issues[key] = {
                "key": key,
                "summary": str(fields.get("summary") or ""),
                "description": str(fields.get("description") or ""),
                "project": _jira_field_name(fields.get("project"), "DEMO"),
                "issue_type": _jira_field_name(fields.get("issuetype"), "Bug"),
                "priority": _jira_field_name(fields.get("priority"), "Not set"),
                "assignee": _jira_field_name(fields.get("assignee"), "Unassigned"),
                "reporter": _jira_field_name(fields.get("reporter"), "Kventin Agent"),
                "environment": str(fields.get("environment") or "Playwright / Chromium"),
                "release": self.release,
                "created": datetime.now(timezone.utc).isoformat(),
                "status": "Open",
                "resolution": None,
                "labels": list(fields.get("labels") or []),
                "comments": [],
                "attachments": [],
                "changelog": [],
            }
            return key

    def assign(self, key: str, assignee: str) -> bool:
        with self._lock:
            issue = self._issues.get(key)
            if not issue:
                return False
            issue["assignee"] = assignee or "Unassigned"
            return True

    def add_attachment(self, key: str, name: str) -> bool:
        with self._lock:
            issue = self._issues.get(key)
            if not issue:
                return False
            issue["attachments"].append(name or "evidence")
            return True

    def add_comment(self, key: str, body: str) -> bool:
        with self._lock:
            issue = self._issues.get(key)
            if not issue:
                return False
            issue["comments"].append(body)
            return True

    def transition(self, key: str, transition_id: str, fields: Optional[Dict[str, Any]] = None) -> bool:
        status_by_transition = {"31": "QA", "41": "In Progress", "51": "Closed"}
        target = status_by_transition.get(str(transition_id))
        if not target:
            return False
        with self._lock:
            issue = self._issues.get(key)
            if not issue:
                return False
            previous = issue["status"]
            issue["status"] = target
            if target == "Closed":
                resolution = ((fields or {}).get("resolution") or {}).get("name")
                issue["resolution"] = resolution or "Fixed"
            issue["changelog"].append({
                "author": {"name": "kventin-agent"},
                "items": [{"field": "status", "fromString": previous, "toString": target}],
            })
            return True

    def mark_ready_for_qa(self, key: str) -> bool:
        with self._lock:
            issue = self._issues.get(key)
            if not issue:
                return False
            previous = issue["status"]
            issue["status"] = "Ready for QA"
            issue["changelog"].append({
                "author": {"name": "demo-developer"},
                "items": [{"field": "status", "fromString": previous, "toString": "Ready for QA"}],
            })
            return True

    def issue_for_api(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            issue = deepcopy(self._issues.get(key))
        if not issue:
            return None
        return {
            "key": issue["key"],
            "fields": {
                "summary": issue["summary"],
                "description": issue["description"],
                "status": {"name": issue["status"]},
                "project": {"key": issue["project"]},
                "issuetype": {"name": issue["issue_type"]},
                "priority": {"name": issue["priority"]},
                "assignee": {"name": issue["assignee"]},
                "reporter": {"name": issue["reporter"]},
                "environment": issue["environment"],
                "labels": issue["labels"],
                "created": issue["created"],
                "comment": {"comments": [{"body": text} for text in issue["comments"]]},
                "attachment": [{"filename": name} for name in issue["attachments"]],
            },
            "changelog": {"histories": issue["changelog"]},
        }

    def search(self, jql: str) -> List[Dict[str, Any]]:
        status_match = re.search(r'status\s*=\s*"([^"]+)"', jql or "", flags=re.I)
        wanted_status = status_match.group(1).casefold() if status_match else ""
        with self._lock:
            issues = [deepcopy(issue) for issue in self._issues.values()]
        if wanted_status:
            issues = [issue for issue in issues if issue["status"].casefold() == wanted_status]
        return [
            {
                "key": issue["key"],
                "fields": {
                    "summary": issue["summary"],
                    "status": {"name": issue["status"]},
                },
            }
            for issue in issues
        ]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "release": self.release,
                "issues": [deepcopy(issue) for issue in self._issues.values()],
            }


class DemoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], state: DemoState) -> None:
        super().__init__(address, DemoRequestHandler)
        self.state = state


class DemoRequestHandler(BaseHTTPRequestHandler):
    server: DemoHTTPServer

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, ValueError):
            return {}

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file():
            self._json(404, {"message": "Not found"})
            return
        self._send_bytes(200, path.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self._serve_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/version":
            self._json(200, {"release": self.server.state.snapshot()["release"]})
            return
        if path == "/debug/issues":
            page = render_debug_issues_page(self.server.state.snapshot())
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/__debug/issues":
            self._json(200, self.server.state.snapshot())
            return
        if path == "/llm/v1/models":
            self._json(200, {"data": [{"id": "demo-disabled-model"}]})
            return
        if path == "/rest/api/2/search":
            query = parse_qs(parsed.query)
            issues = self.server.state.search((query.get("jql") or [""])[0])
            self._json(200, {"issues": issues, "total": len(issues)})
            return

        issue_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)", path)
        if issue_match:
            issue = self.server.state.issue_for_api(issue_match.group(1))
            self._json(200 if issue else 404, issue or {"message": "Issue not found"})
            return
        transition_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)/transitions", path)
        if transition_match:
            self._json(200, {"transitions": [
                {"id": "31", "name": "Start QA", "to": {"name": "QA"}},
                {"id": "41", "name": "Reopen", "to": {"name": "In Progress"}},
                {"id": "51", "name": "Resolve", "to": {"name": "Closed"}},
            ]})
            return
        self._json(404, {"message": "Not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/register":
            status, payload = self.server.state.register(self._read_json())
            self._json(status, payload)
            return
        if path == "/__debug/deploy-fix":
            self._read_body()
            self.server.state.deploy_fix()
            self._json(200, {"release": FIXED_RELEASE})
            return
        if path == "/llm/v1/chat/completions":
            self._read_body()
            self._json(503, {"error": "LLM intentionally disabled for deterministic demo"})
            return
        if path == "/rest/api/2/issue":
            payload = self._read_json()
            key = self.server.state.create_issue(payload.get("fields") or {})
            self._json(201, {"key": key})
            return

        attachment_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)/attachments", path)
        if attachment_match:
            raw = self._read_body()
            name_match = re.search(br'filename="([^"]+)"', raw[:4000])
            name = name_match.group(1).decode("utf-8", errors="replace") if name_match else "evidence"
            ok = self.server.state.add_attachment(attachment_match.group(1), name)
            self._json(200 if ok else 404, [{"filename": name}] if ok else {"message": "Issue not found"})
            return
        comment_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)/comment", path)
        if comment_match:
            payload = self._read_json()
            ok = self.server.state.add_comment(comment_match.group(1), str(payload.get("body") or ""))
            self._json(201 if ok else 404, {"id": "1"} if ok else {"message": "Issue not found"})
            return
        transition_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)/transitions", path)
        if transition_match:
            payload = self._read_json()
            transition_id = str((payload.get("transition") or {}).get("id") or "")
            ok = self.server.state.transition(
                transition_match.group(1), transition_id, payload.get("fields") or {}
            )
            self._json(200 if ok else 400, {} if ok else {"message": "Unknown transition"})
            return
        self._read_body()
        self._json(404, {"message": "Not found", "path": path})

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        assignee_match = re.fullmatch(r"/rest/api/2/issue/([^/]+)/assignee", path)
        if assignee_match:
            payload = self._read_json()
            assignee = next(
                (str(payload.get(name)) for name in ("name", "accountId", "emailAddress") if payload.get(name)),
                "Unassigned",
            )
            ok = self.server.state.assign(assignee_match.group(1), assignee)
            self._json(200 if ok else 404, {} if ok else {"message": "Issue not found"})
            return
        self._read_body()
        self._json(404, {"message": "Not found", "path": path})


class RegistrationDemoServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = DemoState()
        self.httpd = DemoHTTPServer((host, int(port)), self.state)
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%s" % (host, port)

    def start(self) -> "RegistrationDemoServer":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="registration-demo-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description="Registration demo with debug Jira")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = RegistrationDemoServer(args.host, args.port).start()
    print("Registration demo: %s" % server.base_url)
    print("Debug Jira:       %s/debug/issues" % server.base_url)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
