#!/usr/bin/env python3
"""Record one continuous Kventin discover -> fix -> retest demo video."""

import argparse
import html
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples.registration_demo.server import FIXED_RELEASE, RegistrationDemoServer
from scripts.run_registration_demo import _configure_demo_environment, _find_registration_issue


DEMO_INIT_SCRIPT = r"""
(() => {
  const read = (key) => {
    try { return localStorage.getItem(key) || ''; } catch (_) { return ''; }
  };
  const install = () => {
    if (!document.body || document.getElementById('kventin-video-ui')) return;
    const style = document.createElement('style');
    style.setAttribute('data-agent-host', 'video-style');
    style.textContent = `
      [data-kventin-video-target="true"] {
        outline: 4px solid #e5484d !important;
        outline-offset: 4px !important;
      }
      #kventin-video-ui {
        position: fixed; inset: 18px 18px auto auto; z-index: 2147483646;
        width: min(430px, calc(100vw - 36px)); pointer-events: none;
        color: #17202a; font: 14px/1.35 Inter, system-ui, sans-serif;
      }
      #kventin-video-banner {
        display: none; border: 1px solid rgba(8,127,140,.35); border-radius: 8px;
        background: rgba(255,255,255,.96); box-shadow: 0 12px 30px rgba(23,32,42,.2);
        padding: 14px 16px;
      }
      #kventin-video-title {font-size: 16px; font-weight: 800; color: #087f8c;}
      #kventin-video-detail {margin-top: 4px; color: #52606d;}
      #kventin-video-cursor {
        position: fixed; left: 0; top: 0; z-index: 2147483647; width: 22px; height: 22px;
        border: 3px solid white; border-radius: 50%; background: #e5484d;
        box-shadow: 0 3px 12px rgba(0,0,0,.35); opacity: 0;
        transform: translate(-50%, -50%); transition: left .45s ease, top .45s ease, opacity .15s;
      }
    `;
    document.head.appendChild(style);

    const host = document.createElement('div');
    host.id = 'kventin-video-ui';
    host.setAttribute('data-agent-host', 'video-ui');
    host.innerHTML = `
      <div id="kventin-video-banner">
        <div id="kventin-video-title"></div>
        <div id="kventin-video-detail"></div>
      </div>
      <div id="kventin-video-cursor"></div>
    `;
    document.body.appendChild(host);

    const titleNode = host.querySelector('#kventin-video-title');
    const detailNode = host.querySelector('#kventin-video-detail');
    const banner = host.querySelector('#kventin-video-banner');
    const cursor = host.querySelector('#kventin-video-cursor');

    const setPhase = (title, detail) => {
      titleNode.textContent = title || '';
      detailNode.textContent = detail || '';
      banner.style.display = title ? 'block' : 'none';
    };
    window.__kventinDemoSetPhase = setPhase;
    window.__kventinDemoAction = (action) => {
      const verb = String(action.action || 'action').toUpperCase();
      const detail = String(action.reason || action.selector || '').slice(0, 160);
      setPhase(read('kventinVideoPhase') || 'Kventin выполняет действие', `${verb}: ${detail}`);
    };
    window.__kventinDemoFocus = (el) => {
      if (!el || !el.getBoundingClientRect) return false;
      document.querySelectorAll('[data-kventin-video-target="true"]').forEach(node => {
        node.removeAttribute('data-kventin-video-target');
      });
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return false;
      el.setAttribute('data-kventin-video-target', 'true');
      cursor.style.left = `${rect.left + rect.width / 2}px`;
      cursor.style.top = `${rect.top + rect.height / 2}px`;
      cursor.style.opacity = '1';
      const label = (
        (el.labels && el.labels[0] && el.labels[0].innerText)
        || el.innerText || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || el.tagName
      ).replace(/\s+/g, ' ').trim().slice(0, 120);
      detailNode.textContent = `Элемент: ${label}`;
      return true;
    };
    setPhase(read('kventinVideoPhase'), read('kventinVideoDetail'));
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
"""


def _show_card(page: Any, eyebrow: str, title: str, body: str, badge: str) -> None:
    page.set_content(
        """<!doctype html><html lang="ru"><meta charset="utf-8"><style>
        *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
        background:#f5f7fa;color:#17202a;font:18px/1.5 Inter,system-ui,sans-serif}
        main{width:min(840px,calc(100%% - 72px));border-left:7px solid #087f8c;padding:12px 0 12px 38px}
        p{margin:0}.eyebrow{color:#087f8c;font-size:17px;font-weight:800}.badge{display:inline-block;
        margin-top:28px;border:1px solid #b7c3cf;border-radius:999px;background:white;padding:7px 13px;
        font-size:14px;font-weight:750}h1{margin:16px 0 18px;font-size:54px;line-height:1.08;letter-spacing:0}
        .body{max-width:720px;color:#52606d;font-size:22px}</style><main>
        <p class="eyebrow">%s</p><h1>%s</h1><p class="body">%s</p><span class="badge">%s</span>
        </main></html>""" % tuple(html.escape(value) for value in (eyebrow, title, body, badge))
    )


def _set_phase(page: Any, title: str, detail: str = "") -> None:
    try:
        page.evaluate(
            """([title, detail]) => {
                try {
                    localStorage.setItem('kventinVideoPhase', title);
                    localStorage.setItem('kventinVideoDetail', detail);
                } catch (_) {}
                if (window.__kventinDemoSetPhase) window.__kventinDemoSetPhase(title, detail);
            }""",
            [title, detail],
        )
    except Exception:
        pass


def _install_visual_hooks() -> Dict[str, Any]:
    import agent.actions.browser_actions as browser_actions
    import agent.core.agent as agent_module
    import agent.defects.defect_retest as defect_retest

    original_scroll = browser_actions.scroll_to_center
    original_execute = browser_actions.execute_action

    def demo_scroll(locator: Any, page: Any) -> None:
        original_scroll(locator, page)
        try:
            focused = locator.evaluate(
                "el => window.__kventinDemoFocus ? window.__kventinDemoFocus(el) : false"
            )
            if focused:
                time.sleep(0.65)
        except Exception:
            pass

    def demo_execute(page: Any, action: Dict[str, Any], memory: Any) -> str:
        try:
            page.evaluate(
                "action => window.__kventinDemoAction && window.__kventinDemoAction(action)",
                {
                    "action": action.get("action") or "",
                    "selector": action.get("selector") or "",
                    "reason": action.get("reason") or "",
                },
            )
        except Exception:
            pass
        return original_execute(page, action, memory)

    browser_actions.scroll_to_center = demo_scroll
    browser_actions.execute_action = demo_execute
    agent_module.execute_action = demo_execute
    defect_retest.execute_action = demo_execute
    return {
        "agent_module": agent_module,
        "defect_retest": defect_retest,
        "browser_actions": browser_actions,
    }


def _attach_observers(page: Any, console_log: List[Dict[str, Any]], network: List[Dict[str, Any]]) -> None:
    def on_console(message: Any) -> None:
        entry = {"type": message.type, "text": message.text}
        try:
            location = message.location or {}
            if location.get("url"):
                entry["source_url"] = location["url"]
        except Exception:
            pass
        console_log.append(entry)

    def on_page_error(error: Any) -> None:
        console_log.append({"type": "pageerror", "text": str(error)})

    def on_response(response: Any) -> None:
        try:
            if response.status >= 400:
                network.append({
                    "status": response.status,
                    "method": response.request.method,
                    "url": response.url,
                })
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("response", on_response)
    page._agent_console_log = console_log
    page._agent_network_failures = network


def _discover_defect(page: Any, context: Any, base_url: str, state: Any) -> tuple:
    from agent.actions.action_selection import apply_preflight_or_fallback
    from agent.browser.network_capture import NetworkCapture
    from agent.browser.page_analyzer import detect_active_overlays, get_dom_summary
    from agent.browser.screenshot import take_screenshot_b64
    from agent.core.agent_memory import AgentMemory
    from agent.core.local_policy import choose_local_action
    from agent.core.post_analysis import _flush_pending_analysis, _step_post_analysis

    hooks = _install_visual_hooks()
    agent_module = hooks["agent_module"]
    browser_actions = hooks["browser_actions"]

    console_log: List[Dict[str, Any]] = []
    network: List[Dict[str, Any]] = []
    _attach_observers(page, console_log, network)

    memory = AgentMemory()
    memory.session_start = datetime.now()
    memory.set_start_url_for_nav(base_url)
    memory.set_current_url_pattern(base_url)
    browser_actions.set_current_agent_memory(memory)

    capture = NetworkCapture()
    capture.attach(page)
    page._agent_net_capture = capture
    memory.net_capture = capture

    page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
    get_dom_summary(page, max_length=5000)
    time.sleep(1.2)

    for _ in range(7):
        step = memory.begin_step()
        memory.defects_on_current_step = 0
        current_url = page.url
        memory.set_current_url_pattern(current_url)
        get_dom_summary(page, max_length=5000)
        overlay = detect_active_overlays(page)
        has_overlay = bool(overlay.get("has_overlay"))

        action = choose_local_action(
            page,
            memory,
            has_overlay=has_overlay,
            overlay_info=overlay,
        )
        candidates = getattr(memory, "_last_action_candidates", []) or []
        action, _, _ = apply_preflight_or_fallback(
            page=page,
            memory=memory,
            action=action,
            source="VideoDemo",
            has_overlay=has_overlay,
            decision_candidates=candidates,
            fallback_factory=lambda: choose_local_action(
                page,
                memory,
                has_overlay=has_overlay,
                overlay_info=overlay,
            ),
            step=step,
        )

        action_type = (action.get("action") or "").lower()
        selector = (action.get("selector") or "").strip()
        value = (action.get("value") or "").strip()
        _set_phase(
            page,
            "Автономное тестирование",
            "Шаг %d: %s" % (step, action.get("reason") or action_type),
        )
        memory.screenshot_before_action = take_screenshot_b64(page)
        memory.snapshot_logs_before_action(console_log, network)
        result = agent_module._step_execute(page, action, step, memory, context)
        _step_post_analysis(
            page,
            step,
            action,
            result,
            action_type,
            selector,
            value,
            action.get("expected_outcome") or "",
            None,
            has_overlay,
            current_url,
            [],
            console_log,
            network,
            memory,
        )
        _flush_pending_analysis(page, memory, console_log, network)

        if any(int(item.get("status") or 0) >= 500 for item in network[memory.network_len_before_action:]):
            _set_phase(page, "Обнаружен дефект", "POST /api/register вернул HTTP 500")
            time.sleep(2.2)

        if memory.defects_on_current_step:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not state.snapshot()["issues"]:
                _flush_pending_analysis(page, memory, console_log, network)
                time.sleep(0.15)
            break
        time.sleep(0.8)

    _flush_pending_analysis(
        page,
        memory,
        console_log,
        network,
        wait_for_all=True,
    )
    for future in list(getattr(memory, "pending_defect_futures", []) or []):
        future.result(timeout=20)

    snapshot = state.snapshot()
    issue = _find_registration_issue(snapshot)
    return issue, memory, console_log, network, hooks


def record_demo(output_path: Path, artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    raw_video_dir = artifact_dir / "video-raw"
    raw_video_dir.mkdir(parents=True, exist_ok=True)

    server = RegistrationDemoServer().start()
    base_url = server.base_url
    _configure_demo_environment(base_url, artifact_dir, max_steps=7)
    os.environ.update({
        "HEADLESS": "true",
        "VIEWPORT_WIDTH": "1440",
        "VIEWPORT_HEIGHT": "900",
        "BROWSER_SLOW_MO": "80",
        "HIGHLIGHT_DURATION_MS": "0",
        "ACTION_RETRY_COUNT": "0",
        "ENABLE_ORACLE_AFTER_ACTION": "false",
        "ENABLE_SECOND_PASS_BUG": "false",
    })

    video = None
    shutdown_bg_pool_fn = None
    try:
        from playwright.sync_api import sync_playwright

        from agent.browser.browser_options import build_browser_launch_options
        from agent.core.bg_pool import shutdown_bg_pool
        from agent.defects.jira_client import reset_session_defects, start_qa_transition

        reset_session_defects()
        shutdown_bg_pool_fn = shutdown_bg_pool
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **build_browser_launch_options(engine_name="chromium")
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                record_video_dir=str(raw_video_dir),
                record_video_size={"width": 1440, "height": 900},
            )
            context.add_init_script(DEMO_INIT_SCRIPT)
            page = context.new_page()
            video = page.video
            try:
                _show_card(
                    page,
                    "Kventin demo",
                    "Автономный поиск и ретест дефекта",
                    "Форма регистрации, автоматическое заведение тикета и подтверждение исправления.",
                    "Реальный Playwright-прогон",
                )
                time.sleep(2.8)

                issue, memory, console_log, network, hooks = _discover_defect(
                    page, context, base_url, server.state
                )
                key = issue["key"]
                _set_phase(page, "Дефект создан автоматически", "%s: HTTP 500 на регистрации" % key)
                time.sleep(1.4)

                page.goto(base_url + "/debug/issues", wait_until="domcontentloaded")
                _set_phase(
                    page,
                    "Полноценный дефект создан",
                    "%s: Bug / Critical, исполнитель, labels и evidence" % key,
                )
                time.sleep(3.4)

                steps_section = page.locator(
                    "[data-section='Шаги воспроизведения']"
                ).first
                steps_section.scroll_into_view_if_needed()
                _set_phase(
                    page,
                    "Описание и воспроизведение",
                    "Описание проблемы, ОР / ФР и точные шаги сохранены в %s" % key,
                )
                time.sleep(4.0)

                _set_phase(page, "Исправление", "Разворачивается release %s" % FIXED_RELEASE)
                time.sleep(1.2)
                server.state.deploy_fix()
                if not server.state.mark_ready_for_qa(key):
                    raise RuntimeError("Could not move demo issue to Ready for QA")
                page.reload(wait_until="domcontentloaded")
                page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                _set_phase(page, "Исправление развёрнуто", "%s переведён в Ready for QA" % key)
                time.sleep(3.2)

                if not start_qa_transition(key):
                    raise RuntimeError("Could not start QA transition")
                page.reload(wait_until="domcontentloaded")
                _set_phase(page, "Автоматический ретест", "%s взят агентом в QA" % key)
                time.sleep(2.0)

                retest = hooks["defect_retest"]
                processed = retest.process_retest_issue_on_current_page(
                    key,
                    page,
                    memory,
                    console_log,
                    network,
                    fallback_start_url=base_url,
                )
                if not processed:
                    raise RuntimeError("Retest was not processed")
                time.sleep(2.4)

                final_issue = _find_registration_issue(server.state.snapshot())
                if final_issue.get("status") != "Closed" or final_issue.get("resolution") != "Fixed":
                    raise RuntimeError("Retest did not close issue as Fixed")

                page.goto(base_url + "/debug/issues", wait_until="domcontentloaded")
                _set_phase(page, "Ретест пройден", "%s закрыт: Closed / Fixed" % key)
                time.sleep(3.4)
                page.locator(".latest-comment").first.scroll_into_view_if_needed()
                _set_phase(
                    page,
                    "Протокол ретеста сохранён",
                    "В комментарии зафиксированы сценарий и успешный результат",
                )
                time.sleep(3.4)

                _show_card(
                    page,
                    "Kventin demo",
                    "Дефект найден и исправление подтверждено",
                    "%s: Open → Ready for QA → QA → Closed / Fixed" % key,
                    "SUCCESS",
                )
                time.sleep(2.8)
            finally:
                try:
                    context.close()
                    if video is not None:
                        video.save_as(str(output_path))
                finally:
                    browser.close()

        if video is None:
            raise RuntimeError("Playwright did not create a video object")
        if not output_path.is_file() or output_path.stat().st_size < 10000:
            raise RuntimeError("Recorded video is missing or too small")
        return output_path
    finally:
        try:
            if shutdown_bg_pool_fn is not None:
                shutdown_bg_pool_fn(wait=True, pool_names=("jira", "analysis"))
        finally:
            server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the registration defect lifecycle demo")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "registration-demo",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "artifacts" / "registration-demo" / "kventin-registration-demo.webm",
    )
    args = parser.parse_args()
    try:
        path = record_demo(args.output.resolve(), args.artifacts_dir.resolve())
    except Exception as exc:
        print("[video-demo] FAILED: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print("[video-demo] SUCCESS: %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
