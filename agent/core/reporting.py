"""Session metrics and report rendering."""
from __future__ import annotations

import html as html_module
import logging
import os
from datetime import datetime
from typing import List

from playwright.sync_api import Page

from agent.actions.action_result import action_failed
from agent.core.agent_memory import AgentMemory

LOG = logging.getLogger("kventin.reporting")

def _collect_browser_metrics(page: Page, memory: AgentMemory, step: int) -> None:
    """Собрать метрики: загрузка страницы, ресурсы по типам, время отклика, память."""
    try:
        url = page.url[:200] if page.url else ""
        metrics = page.evaluate("""() => {
            const out = { url: window.location.href ? window.location.href.slice(0, 200) : '' };
            out.page = {};
            out.resources = {};
            out.response = {};
            try {
                const t = performance.timing || {};
                const nav = performance.getEntriesByType('navigation')[0];
                const toMs = (a, b) => (a > 0 && b >= 0) ? Math.round(a - b) : null;
                if (nav) {
                    out.page.ttfb = toMs(nav.responseStart, nav.fetchStart);
                    out.page.domInteractive = toMs(nav.domInteractive, nav.fetchStart);
                    out.page.domContentLoaded = toMs(nav.domContentLoadedEventEnd, nav.fetchStart);
                    out.page.loadComplete = toMs(nav.loadEventEnd, nav.fetchStart);
                    out.page.domComplete = toMs(nav.domComplete, nav.fetchStart);
                    out.loadEventEnd = out.page.loadComplete;
                    out.domContentLoaded = out.page.domContentLoaded;
                    out.domComplete = out.page.domComplete;
                    out.responseStart = out.page.ttfb;
                } else if (t.loadEventEnd) {
                    const start = t.navigationStart;
                    out.page.ttfb = t.responseStart - start;
                    out.page.domInteractive = t.domInteractive - start;
                    out.page.domContentLoaded = t.domContentLoadedEventEnd - start;
                    out.page.loadComplete = t.loadEventEnd - start;
                    out.page.domComplete = t.domComplete - start;
                    out.loadEventEnd = out.page.loadComplete;
                    out.domContentLoaded = out.page.domContentLoaded;
                    out.domComplete = out.page.domComplete;
                    out.responseStart = out.page.ttfb;
                }
                const paint = performance.getEntriesByType('paint');
                paint.forEach(p => {
                    if (p.name === 'first-paint') out.page.firstPaint = Math.round(p.startTime);
                    if (p.name === 'first-contentful-paint') out.page.firstContentfulPaint = Math.round(p.startTime);
                });
            } catch (e) {}
            try {
                out.scrollHeight = document.documentElement ? document.documentElement.scrollHeight : 0;
                out.scrollWidth = document.documentElement ? document.documentElement.scrollWidth : 0;
                out.bodyChildren = document.body ? document.body.childElementCount : 0;
                out.readyState = document.readyState || '';
            } catch (e) {}
            try {
                const resources = performance.getEntriesByType('resource');
                const byType = {};
                let xhrFetch = [];
                resources.forEach(r => {
                    const type = (r.initiatorType || 'other').toLowerCase();
                    if (!byType[type]) byType[type] = { count: 0, durationSum: 0, durationMax: 0, transferSum: 0, items: [] };
                    const d = Math.round(r.duration || 0);
                    const sz = r.transferSize || 0;
                    byType[type].count++;
                    byType[type].durationSum += d;
                    byType[type].durationMax = Math.max(byType[type].durationMax, d);
                    byType[type].transferSum += sz;
                    if (d > 0) byType[type].items.push({ n: (r.name || '').slice(-80), d, sz });
                    if ((type === 'xmlhttprequest' || type === 'fetch') && r.responseStart > 0) {
                        const resp = Math.round((r.responseEnd || r.startTime) - r.responseStart);
                        xhrFetch.push({ n: (r.name || '').slice(-60), ms: resp });
                    }
                });
                Object.keys(byType).forEach(k => {
                    const x = byType[k];
                    x.avgDuration = x.count ? Math.round(x.durationSum / x.count) : 0;
                    x.slowest = x.items.sort((a, b) => b.d - a.d).slice(0, 3).map(i => ({ name: i.n, duration: i.d, size: i.sz }));
                });
                out.resources = byType;
                xhrFetch.sort((a, b) => b.ms - a.ms);
                out.response.xhrFetch = xhrFetch.slice(0, 10);
                if (xhrFetch.length) {
                    out.response.avgMs = Math.round(xhrFetch.reduce((s, i) => s + i.ms, 0) / xhrFetch.length);
                    out.response.maxMs = xhrFetch[0] ? xhrFetch[0].ms : 0;
                }
                const lcp = performance.getEntriesByType('largest-contentful-paint');
                if (lcp.length) out.page.lcp = Math.round(lcp[lcp.length - 1].startTime);
            } catch (e) {}
            try {
                if (performance.memory) {
                    out.usedJSHeapSize = performance.memory.usedJSHeapSize;
                    out.totalJSHeapSize = performance.memory.totalJSHeapSize;
                }
            } catch (e) {}
            return out;
        }""")
        if isinstance(metrics, dict):
            metrics["step"] = step
            metrics["url"] = metrics.get("url") or url
            memory._browser_metrics_latest = metrics
            memory._browser_metrics_history.append(dict(metrics))
            if len(memory._browser_metrics_history) > 50:
                memory._browser_metrics_history.pop(0)
    except Exception as e:
        LOG.debug("collect_browser_metrics: %s", e)

def _write_junit_report(memory: AgentMemory, path: str) -> None:
    """Записать отчёт в формате JUnit XML."""
    step_log = getattr(memory, "_step_log", None) or []
    failures = sum(1 for entry in step_log if action_failed(entry.get("result")))
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    duration_sec = 0.0
    if getattr(memory, "session_start", None):
        duration_sec = (datetime.now() - memory.session_start).total_seconds()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="Kventin" tests="{len(step_log)}" failures="{failures}" errors="0" skipped="0" time="{duration_sec:.2f}" timestamp="{ts}">',
    ]
    for e in step_log:
        step = e.get("step", 0)
        result = (e.get("result") or "")
        fail = action_failed(result)
        name = html_module.escape(f"step_{step}_{e.get('action', '')}")
        res_esc = html_module.escape(result[:500])
        if fail:
            lines.append(f'  <testcase name="{name}"><failure message="{res_esc}"/></testcase>')
        else:
            lines.append(f'  <testcase name="{name}"><system-out>{res_esc}</system-out></testcase>')
    lines.append("</testsuite>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def _build_html_report(memory: AgentMemory, report_text: str, start_url: str = "", video_dir: str = "") -> str:
    """Собрать красивый HTML-отчёт сессии."""
    def esc(s: str) -> str:
        return html_module.escape(str(s) if s else "", quote=True)

    if not memory.session_start:
        duration_sec = 0
    else:
        duration_sec = (datetime.now() - memory.session_start).total_seconds()
    step_log = getattr(memory, "_step_log", None) or []
    defects = getattr(memory, "defects_created", None) or []
    coverage = ", ".join(str(z) for z in memory.coverage_zones) if memory.coverage_zones else "—"

    steps_rows = []
    for e in step_log:
        sp = e.get("screenshot_path") or ""
        img_cell = ""
        if sp and not os.path.isabs(sp) and "screenshots/" in sp:
            img_cell = f'<a href="{esc(sp)}" target="_blank"><img src="{esc(sp)}" alt="шаг" class="step-thumb"/></a>'
        fok, ftot = e.get("flakiness_ok"), e.get("flakiness_total")
        flak_cell = f"{fok}/{ftot}" if (fok is not None and ftot) else "—"
        step_num = e.get("step", "")
        steps_rows.append(
            f"<tr id=\"step-{esc(str(step_num))}\"><td>{step_num}</td><td class=\"url\">{esc((e.get('url') or '')[:80])}</td>"
            f"<td><span class=\"act act-{esc(e.get('action', ''))}\">{esc(e.get('action', ''))}</span></td>"
            f"<td class=\"sel\">{esc((e.get('selector') or '')[:50])}</td>"
            f"<td class=\"result\">{esc((e.get('result') or '')[:80])}</td>"
            f"<td><span class=\"src src-{esc(e.get('source', ''))}\">{esc(e.get('source', ''))}</span></td>"
            f"<td class=\"sub\">{flak_cell}</td><td class=\"thumb\">{img_cell}</td></tr>"
        )
    steps_body = "\n".join(steps_rows) if steps_rows else "<tr><td colspan=\"8\">Нет данных</td></tr>"

    defects_rows = []
    for d in defects:
        key = d.get("key", "")
        summary = d.get("summary", "")[:120]
        sev = d.get("severity", "major")
        defects_rows.append(f"<tr><td class=\"key\">{esc(key)}</td><td><span class=\"sev sev-{esc(sev)}\">{esc(sev)}</span></td><td>{esc(summary)}</td></tr>")
    defects_body = "\n".join(defects_rows) if defects_rows else "<tr><td colspan=\"3\">Нет</td></tr>"

    nav_graph = getattr(memory, "_nav_graph", None) or []
    nav_rows = []
    for edge in nav_graph[-100:]:
        frm = (edge.get("from_url") or "")[:60]
        to = (edge.get("to_url") or "")[:60]
        step = edge.get("step", "")
        nav_rows.append(f"<tr><td>{step}</td><td class=\"url\">{esc(frm)}</td><td class=\"url\">→ {esc(to)}</td></tr>")
    nav_body = "\n".join(nav_rows) if nav_rows else "<tr><td colspan=\"3\">Нет переходов</td></tr>"

    broken_links = getattr(memory, "_broken_links", None) or []
    broken_rows = []
    for b in broken_links[-80:]:
        url_short = (b.get("url") or "")[:100]
        status = b.get("status") or ""
        err = (b.get("error") or "")[:80]
        broken_rows.append(f"<tr><td class=\"url\">{esc(url_short)}</td><td>{status}</td><td class=\"result\">{esc(err)}</td></tr>")
    broken_body = "\n".join(broken_rows) if broken_rows else "<tr><td colspan=\"3\">Нет</td></tr>"
    broken_section = (
        "<section><h2>Битые ссылки</h2><table><thead><tr><th>URL</th><th>Статус</th><th>Ошибка</th></tr></thead>"
        "<tbody>" + broken_body + "</tbody></table></section>"
    ) if broken_rows else ""

    console_warnings = getattr(memory, "_session_console_warnings", None) or []
    console_errors = [c for c in console_warnings if (c.get("type") or "").lower() == "error"]
    cw_rows = []
    for c in console_errors[-50:]:
        cw_rows.append(f"<tr><td><span class=\"sev sev-{esc(c.get('type', 'error'))}\">{esc(c.get('type', ''))}</span></td><td class=\"result\">{esc((c.get('text') or '')[:150])}</td></tr>")
    cw_body = "\n".join(cw_rows) if cw_rows else "<tr><td colspan=\"2\">Нет ошибок</td></tr>"
    console_section = (
        "<section><h2>Консоль (ошибки)</h2><table><thead><tr><th>Тип</th><th>Текст</th></tr></thead>"
        "<tbody>" + cw_body + "</tbody></table></section>"
    ) if cw_rows else ""

    mixed_content = getattr(memory, "_mixed_content", None) or []
    mc_body = "<br/>".join(esc((m.get("url") or "")[:80]) for m in mixed_content[-20:]) if mixed_content else "—"
    ws_issues = getattr(memory, "_websocket_issues", None) or []
    ws_body = "<br/>".join(f"{esc((w.get('url') or '')[:60])} ({w.get('event', '')})" for w in ws_issues[-20:]) if ws_issues else "—"
    mixed_section = (
        "<section><h2>Mixed content / WebSocket</h2>"
        f"<p><strong>Mixed content:</strong> {mc_body}</p><p><strong>WebSocket:</strong> {ws_body}</p></section>"
    ) if (mixed_content or ws_issues) else ""
    api_log = getattr(memory, "_api_log", None) or []
    def _status_code(x):
        try:
            return int(x.get("status") or 0)
        except (TypeError, ValueError):
            return 0
    api_failed = [a for a in api_log if _status_code(a) >= 400 or not a.get("ok", True)]
    api_rows = []
    for a in api_failed[-50:]:
        method = a.get("method", "")
        url_short = (a.get("url") or "")[:80]
        status = a.get("status", "") or ("—" if not a.get("ok", True) else "")
        cls = "sev sev-major"
        api_rows.append(f"<tr><td>{esc(method)}</td><td class=\"url\">{esc(url_short)}</td><td class=\"{cls}\">{esc(str(status))}</td></tr>")
    api_body = "\n".join(api_rows) if api_rows else "<tr><td colspan=\"3\">Нет запросов с ошибками</td></tr>"
    api_section = (
        "<section><details class=\"api-section\" id=\"api-section\">"
        f"<summary><h2 class=\"api-section-title\">API (XHR/fetch) — только ошибки</h2><span class=\"sub\">{len(api_failed)} запросов с ошибками (4xx, 5xx)</span></summary>"
        "<table class=\"api-table\"><thead><tr><th>Метод</th><th>URL</th><th>Статус</th></tr></thead>"
        "<tbody>" + api_body + "</tbody></table></details></section>"
    ) if api_failed else ""
    visual_regressions = getattr(memory, "_visual_regressions", None) or []
    vr_rows = []
    for v in visual_regressions:
        vr_rows.append(f"<tr><td class=\"url\">{esc((v.get('url') or '')[:80])}</td><td>{v.get('change_percent', 0)}%</td><td class=\"result\">{esc((v.get('detail') or '')[:100])}</td></tr>")
    vr_body = "\n".join(vr_rows) if vr_rows else "<tr><td colspan=\"3\">Нет</td></tr>"
    vr_section = (
        "<section><h2>Visual regression (baseline)</h2><table><thead><tr><th>URL</th><th>Изменение %</th><th>Детали</th></tr></thead>"
        "<tbody>" + vr_body + "</tbody></table></section>"
    ) if vr_rows else ""

    browser_metrics = getattr(memory, "_browser_metrics_latest", None) or {}
    metrics_rows = []
    if browser_metrics:
        m = browser_metrics
        page = m.get("page") or {}
        for key, label in [
            ("ttfb", "TTFB (время до первого байта)"),
            ("domInteractive", "DOM interactive"),
            ("domContentLoaded", "DOM Content Loaded"),
            ("loadComplete", "Полная загрузка (load)"),
            ("firstPaint", "First Paint"),
            ("firstContentfulPaint", "First Contentful Paint"),
            ("lcp", "LCP (Largest Contentful Paint)"),
        ]:
            val = page.get(key)
            if val is not None:
                metrics_rows.append(f"<tr><td>{esc(label)}</td><td>{val} мс</td></tr>")
        res = m.get("resources") or {}
        if res:
            metrics_rows.append("<tr><td colspan=\"2\"><strong>Ресурсы по типам</strong></td></tr>")
            for rtype, data in sorted(res.items(), key=lambda x: (str(x[0]),)):
                count = data.get("count", 0)
                avg = data.get("avgDuration")
                mx = data.get("durationMax")
                kb = (data.get("transferSum") or 0) / 1024
                metrics_rows.append(
                    f"<tr><td class=\"sub\">{esc(rtype)}</td><td>n={count}, avg={avg or '—'} мс, max={mx or '—'} мс, {kb:.0f} КБ</td></tr>"
                )
                for s in (data.get("slowest") or [])[:2]:
                    metrics_rows.append(f"<tr><td></td><td class=\"sub\">↳ {esc(str(s.get('duration', 0)))} мс {esc((s.get('name') or '')[-50:])}</td></tr>")
        resp = m.get("response") or {}
        if resp.get("xhrFetch"):
            metrics_rows.append("<tr><td colspan=\"2\"><strong>XHR/fetch отклик</strong></td></tr>")
            if resp.get("avgMs") is not None:
                metrics_rows.append(f"<tr><td>Среднее / макс</td><td>{resp['avgMs']} / {resp.get('maxMs', '—')} мс</td></tr>")
            for x in (resp.get("xhrFetch") or [])[:3]:
                metrics_rows.append(f"<tr><td></td><td class=\"sub\">↳ {x.get('ms', 0)} мс {esc((x.get('n') or '')[-40:])}</td></tr>")
        if m.get("scrollHeight") is not None:
            metrics_rows.append(f"<tr><td>scrollHeight / scrollWidth</td><td>{m.get('scrollHeight')} / {m.get('scrollWidth', '—')}</td></tr>")
        if m.get("bodyChildren") is not None:
            metrics_rows.append(f"<tr><td>body child elements</td><td>{m['bodyChildren']}</td></tr>")
        if m.get("usedJSHeapSize") is not None:
            used_mb = round(m["usedJSHeapSize"] / 1024 / 1024, 2)
            total_mb = round(m.get("totalJSHeapSize", 0) / 1024 / 1024, 2)
            metrics_rows.append(f"<tr><td>JS heap</td><td>{used_mb} / {total_mb} МБ</td></tr>")
        if m.get("readyState"):
            metrics_rows.append(f"<tr><td>readyState</td><td>{esc(m['readyState'])}</td></tr>")
    metrics_body = "\n".join(metrics_rows) if metrics_rows else "<tr><td colspan=\"2\">Не собраны (открой отчёт после шага с загруженной страницей)</td></tr>"

    # Карточки метрик для сводки (красивое оформление)
    summary_metrics_cards = []
    if browser_metrics:
        page = browser_metrics.get("page") or {}
        for key, label in [
            ("ttfb", "TTFB"), ("domContentLoaded", "DCL"), ("loadComplete", "Load"),
            ("firstContentfulPaint", "FCP"), ("lcp", "LCP"),
        ]:
            val = page.get(key)
            if val is not None:
                summary_metrics_cards.append(f'<div class="card card-metric"><div class="val">{esc(str(val))}</div><div class="lbl">{esc(label)}</div></div>')
        if browser_metrics.get("usedJSHeapSize") is not None:
            used_mb = round(browser_metrics["usedJSHeapSize"] / 1024 / 1024, 1)
            summary_metrics_cards.append(f'<div class="card card-metric"><div class="val">{used_mb}</div><div class="lbl">JS heap МБ</div></div>')
    summary_metrics_html = "\n".join(summary_metrics_cards) if summary_metrics_cards else "<p class=\"sub\">Метрики не собраны</p>"

    total_steps = len(step_log)
    timeline_bars = ""
    if total_steps > 0:
        for e in step_log[-60:]:
            s = e.get("step", 0)
            act = e.get("action", "")
            is_fail = action_failed(e.get("result"))
            pct = 100 * s / max(total_steps, 1)
            cls = "timeline-fail" if is_fail else "timeline-ok"
            timeline_bars += f'<span class="timeline-bar {cls}" style="width:{max(2, 100/60)}%" title="#{s} {act}"/>'

    # Данные для Session Replay (без json.dumps, чтобы избежать проблем с несериализуемыми типами)
    steps_js_items: List[str] = []
    for e in step_log:
        step_val = e.get("step")
        if isinstance(step_val, dict):
            step_num = 0
        else:
            try:
                step_num = int(step_val or 0)
            except (TypeError, ValueError):
                step_num = 0
        sp = e.get("screenshot_path") or ""
        thumb = os.path.basename(sp) if sp else ""
        thumb_safe = thumb.replace("\\", "\\\\").replace('"', '\\"')
        steps_js_items.append(f'{{"step": {step_num}, "thumb": "{thumb_safe}"}}')
    steps_js = "[" + ",".join(steps_js_items) + "]"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="15"/>
<title>Kventin — отчёт сессии</title>
<style>
:root {{
  --bg: #0c0c10;
  --bg2: #12121a;
  --surface: #18181f;
  --surface2: #22222e;
  --surface3: #2a2a38;
  --text: #f0f0f5;
  --text2: #a0a0b0;
  --accent: #6366f1;
  --accent2: #818cf8;
  --accent-dim: rgba(99,102,241,0.12);
  --success: #22c55e;
  --warn: #eab308;
  --danger: #ef4444;
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.35);
  --font: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 0;
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.55;
  background: var(--bg);
  color: var(--text);
  background-image: radial-gradient(ellipse 100% 60% at 50% -30%, rgba(99,102,241,0.12), transparent 55%);
  min-height: 100vh;
}}
.container {{ max-width: 1280px; margin: 0 auto; padding: 0 1.5rem 2rem; }}
.header-bar {{
  background: var(--surface);
  border-bottom: 1px solid var(--surface2);
  padding: 1rem 1.5rem;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}}
.header-bar h1 {{
  margin: 0;
  font-size: 1.5rem;
  font-weight: 700;
  background: linear-gradient(135deg, var(--accent2), var(--accent));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.live-badge {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.75rem;
  border-radius: 999px;
  background: var(--accent-dim);
  color: var(--accent2);
  font-size: 0.8rem;
  font-weight: 500;
}}
.live-badge::before {{
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--success);
  animation: pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
.sub {{
  color: var(--text2);
  font-size: 0.9rem;
  margin-bottom: 0.5rem;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1rem;
  margin-bottom: 2rem;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  text-align: center;
  box-shadow: var(--shadow);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}}
.card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
.card .val {{
  font-size: 1.75rem;
  font-weight: 700;
  color: var(--accent2);
  letter-spacing: -0.02em;
}}
.card .lbl {{ font-size: 0.8rem; color: var(--text2); margin-top: 0.35rem; text-transform: uppercase; letter-spacing: 0.04em; }}
section {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1.5rem 1.75rem;
  margin-bottom: 1.25rem;
  box-shadow: var(--shadow);
}}
section h2 {{
  font-size: 0.95rem;
  margin: 0 0 1rem;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--surface2);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
  border-radius: var(--radius-sm);
  overflow: hidden;
}}
th {{
  text-align: left;
  padding: 0.75rem 1rem;
  color: var(--text2);
  font-weight: 600;
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  background: var(--surface2);
}}
td {{
  padding: 0.7rem 1rem;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  background: var(--surface);
}}
tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
tr:hover td {{ background: rgba(99,102,241,0.06); }}
tr:last-child td {{ border-bottom: none; }}
.url {{ max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.sel {{ max-width: 140px; overflow: hidden; text-overflow: ellipsis; }}
.result {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; }}
.act {{ padding: 0.25em 0.6em; border-radius: var(--radius-sm); font-weight: 500; background: var(--surface3); color: var(--text); font-size: 0.85em; }}
.act-click {{ background: rgba(99,102,241,0.28); color: var(--accent2); }}
.act-type {{ background: rgba(34,197,94,0.22); color: var(--success); }}
.act-scroll {{ background: rgba(234,179,8,0.22); color: var(--warn); }}
.act-hover {{ background: rgba(129,140,248,0.22); color: var(--accent2); }}
.act-close_modal {{ background: rgba(239,68,68,0.22); color: var(--danger); }}
.act-fill_form {{ background: rgba(34,197,94,0.28); color: var(--success); }}
.src {{ padding: 0.2em 0.45em; border-radius: 4px; font-size: 0.78em; }}
.src-llm {{ background: rgba(99,102,241,0.22); color: var(--accent2); }}
.src-fast {{ background: var(--surface3); color: var(--text2); }}
.step-thumb {{ width: 88px; height: 50px; object-fit: cover; border-radius: var(--radius-sm); display: block; }}
.thumb {{ width: 100px; }}
.key {{ font-family: ui-monospace, monospace; color: var(--accent2); }}
.sev {{ padding: 0.25em 0.55em; border-radius: var(--radius-sm); font-size: 0.82em; font-weight: 500; }}
.sev-critical {{ background: rgba(239,68,68,0.28); color: var(--danger); }}
.sev-major {{ background: rgba(234,179,8,0.28); color: var(--warn); }}
.sev-minor {{ background: var(--surface3); color: var(--text2); }}
pre {{ margin: 0; font-size: 0.85rem; color: var(--text2); white-space: pre-wrap; line-height: 1.5; }}
.timeline-wrap {{ display: flex; flex-wrap: wrap; gap: 2px; margin-top: 0.5rem; }}
.timeline-bar {{ height: 22px; border-radius: 5px; display: inline-block; min-width: 5px; }}
.timeline-ok {{ background: linear-gradient(180deg, var(--accent), var(--accent2)); opacity: 0.9; }}
.timeline-fail {{ background: var(--danger); }}
.replay-wrap {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; flex-wrap: wrap; }}
.replay-btn {{ padding: 0.5rem 1rem; border-radius: var(--radius-sm); background: var(--surface2); color: var(--text); border: 1px solid var(--surface3); cursor: pointer; font-size: 0.9rem; }}
.replay-btn:hover {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.replay-strip {{ display: flex; flex-wrap: wrap; gap: 6px; max-height: 130px; overflow-y: auto; }}
.replay-thumb {{ width: 84px; height: 47px; border-radius: var(--radius-sm); overflow: hidden; border: 2px solid transparent; cursor: pointer; }}
.replay-thumb.active {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }}
.replay-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.metrics-summary-wrap {{ margin-bottom: 1rem; }}
.cards-inline {{ display: flex; flex-wrap: wrap; gap: 0.75rem; }}
.card-metric .val {{ font-size: 1.25rem; }}
.report-full-text {{ margin-top: 1rem; }}
.report-full-text summary {{ cursor: pointer; color: var(--text2); }}
.api-section-title {{ display: inline; margin-right: 0.5rem; }}
.api-section summary {{ cursor: pointer; list-style: none; }}
.api-section summary::-webkit-details-marker {{ display: none; }}
.api-filter-wrap {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; }}
.api-filter {{ font-size: 0.85rem; }}
.api-filter.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.api-table tr[data-status].hidden {{ display: none; }}
@media (max-width: 768px) {{ .url, .result {{ max-width: 120px; }} .header-bar {{ flex-direction: column; align-items: flex-start; }} }}
</style>
</head>
<body>
<div class="header-bar">
<div>
<h1>Kventin</h1>
<p class="sub">Отчёт сессии · {esc(start_url or "—")[:70]}</p>
</div>
<span class="live-badge">Обновлено {esc(datetime.now().strftime("%H:%M:%S"))}</span>
</div>
<div class="container">
<div class="cards">
<div class="card"><div class="val">{len(step_log)}</div><div class="lbl">Шагов</div></div>
<div class="card"><div class="val">{int(duration_sec)}</div><div class="lbl">Секунд</div></div>
<div class="card"><div class="val">{esc(memory.tester_phase)}</div><div class="lbl">Фаза</div></div>
<div class="card"><div class="val">{len(defects)}</div><div class="lbl">Дефектов</div></div>
<div class="card"><div class="val">{len(memory.done_click)}</div><div class="lbl">Кликов</div></div>
<div class="card"><div class="val">{len(memory.done_type)}</div><div class="lbl">Вводов</div></div>
</div>
{f'<p class="sub">Видео сессии: <code>{esc(video_dir)}</code></p>' if video_dir else ''}
<section>
<h2>Сводка</h2>
<div class="metrics-summary-wrap">
<h3 class="sub">Метрики браузера</h3>
<div class="cards cards-inline">{summary_metrics_html}</div>
</div>
<details class="report-full-text"><summary>Полный текст отчёта</summary>
<pre>{esc(report_text)}</pre>
</details>
</section>
<section>
<h2>Покрытие</h2>
<p>{esc(coverage)}</p>
</section>
<section>
<details class="nav-details"><summary><h2>Навигация</h2></summary>
<table>
<thead><tr><th>Шаг</th><th>От</th><th>Куда</th></tr></thead>
<tbody>
{nav_body}
</tbody>
</table>
</details>
</section>
<section>
<h2>Timeline</h2>
<div class="timeline-wrap">{timeline_bars}</div>
</section>
<section>
<h2>Session Replay</h2>
<div class="replay-wrap" id="replay-wrap">
<button type="button" class="replay-btn" id="replay-prev">◀ Prev</button>
<button type="button" class="replay-btn" id="replay-play">Play</button>
<button type="button" class="replay-btn" id="replay-next">Next ▶</button>
<span class="sub" id="replay-info">Шаг 0 / {total_steps}</span>
</div>
<div class="replay-strip" id="replay-strip"></div>
<script>
(function(){{
var steps = {steps_js};
var idx = 0, total = steps.length, playing = false, t;
if (!total) {{ document.getElementById("replay-info").textContent = "Нет шагов"; }}
else {{
var strip = document.getElementById("replay-strip");
steps.forEach(function(s, i){{
 var a = document.createElement("a");
 a.href = "#step-" + s.step;
 a.className = "replay-thumb" + (i===0 ? " active" : "");
 a.dataset.step = i;
 a.innerHTML = s.thumb ? "<img src=\\"screenshots/" + s.thumb + "\\" alt=\\"#"+s.step+"\\"/>" : "<span>#"+s.step+"</span>";
 strip.appendChild(a);
}});
function go(i){{
 idx = Math.max(0, Math.min(i, total-1));
 strip.querySelectorAll(".replay-thumb").forEach(function(el, j){{ el.classList.toggle("active", j===idx); }});
 document.getElementById("replay-info").textContent = "Шаг " + (steps[idx]&&steps[idx].step) + " / " + total;
 var stepNum = steps[idx] && steps[idx].step;
 if(stepNum) {{ var row = document.getElementById("step-" + stepNum); if(row) row.scrollIntoView({{block:"center"}}); }}
}}
document.getElementById("replay-prev").onclick = function(){{ go(idx-1); }};
document.getElementById("replay-next").onclick = function(){{ go(idx+1); }};
document.getElementById("replay-play").onclick = function(){{
 playing = !playing;
 this.textContent = playing ? "Pause" : "Play";
 if(playing) t = setInterval(function(){{ go(idx+1); if(idx>=total-1) clearInterval(t); }}, 2000);
 else clearInterval(t);
}};
strip.querySelectorAll(".replay-thumb").forEach(function(el){{ el.onclick = function(e){{ e.preventDefault(); go(parseInt(this.dataset.step,10)); }}; }});
}}
}})();
</script>
</section>
{broken_section}
{console_section}
{vr_section}
<section>
<h2>Метрики браузера (последний сбор)</h2>
<table>
<thead><tr><th>Метрика</th><th>Значение</th></tr></thead>
<tbody>{metrics_body}</tbody>
</table>
<p class="sub">Шаг: {browser_metrics.get('step', '—')}, URL: {esc((browser_metrics.get('url') or '')[:120])}</p>
</section>
{api_section}
{mixed_section}
<section>
<h2>Шаги</h2>
<table id="report-steps-table">
<thead><tr><th>#</th><th>URL</th><th>Действие</th><th>Селектор</th><th>Результат</th><th>Источник</th><th>Flakiness</th><th>Скрин</th></tr></thead>
<tbody>
{steps_body}
</tbody>
</table>
</section>
<section>
<h2>Созданные дефекты</h2>
<table>
<thead><tr><th>Ключ</th><th>Severity</th><th>Описание</th></tr></thead>
<tbody>
{defects_body}
</tbody>
</table>
</section>
</div>
</body>
</html>"""

__all__ = ["_build_html_report", "_collect_browser_metrics", "_write_junit_report"]
