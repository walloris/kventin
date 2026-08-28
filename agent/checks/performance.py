"""
Performance-мониторинг: время загрузки, размер ресурсов, тяжёлые запросы.
"""
import logging
from typing import List, Dict, Any

from playwright.sync_api import Page

LOG = logging.getLogger("Perf")

# Пороги
SLOW_PAGE_LOAD_MS = 5000      # страница грузится дольше 5с
SLOW_RESOURCE_MS = 3000        # один ресурс грузится дольше 3с
LARGE_RESOURCE_KB = 2048       # ресурс > 2 МБ


def check_performance(page: Page) -> List[Dict[str, Any]]:
    """
    Собрать метрики производительности. Возвращает список issue:
    [{"type": "performance", "severity": ..., "rule": ..., "detail": ...}]
    """
    issues = []
    issues.extend(_check_page_load_time(page))
    issues.extend(_check_slow_resources(page))
    issues.extend(_check_large_resources(page))
    issues.extend(_check_memory_usage(page))
    return issues


def _check_page_load_time(page: Page) -> List[Dict]:
    """Время загрузки страницы (navigation timing)."""
    try:
        timing = page.evaluate("""() => {
            const t = performance.timing || {};
            const nav = performance.getEntriesByType('navigation')[0] || {};
            return {
                domContentLoaded: nav.domContentLoadedEventEnd || (t.domContentLoadedEventEnd - t.navigationStart),
                loadComplete: nav.loadEventEnd || (t.loadEventEnd - t.navigationStart),
                domInteractive: nav.domInteractive || (t.domInteractive - t.navigationStart),
                ttfb: nav.responseStart || (t.responseStart - t.navigationStart),
            };
        }""")
        issues = []
        load_time = timing.get("loadComplete", 0)
        ttfb = timing.get("ttfb", 0)
        if load_time > SLOW_PAGE_LOAD_MS:
            issues.append({
                "type": "performance", "severity": "warning", "rule": "slow-page-load",
                "detail": f"Страница загружается {load_time}мс (порог {SLOW_PAGE_LOAD_MS}мс). TTFB={ttfb}мс",
            })
        if ttfb > 2000:
            issues.append({
                "type": "performance", "severity": "warning", "rule": "slow-ttfb",
                "detail": f"Время до первого байта (TTFB): {ttfb}мс (> 2с)",
            })
        return issues
    except Exception as e:
        LOG.debug("page-load check: %s", e)
        return []


def _check_slow_resources(page: Page) -> List[Dict]:
    """Медленные ресурсы (> 3с)."""
    try:
        entries = page.evaluate(f"""() => {{
            return performance.getEntriesByType('resource')
                .filter(e => e.duration > {SLOW_RESOURCE_MS})
                .map(e => ({{ name: e.name.slice(0, 120), duration: Math.round(e.duration), type: e.initiatorType }}))
                .slice(0, 10);
        }}""")
        return [
            {"type": "performance", "severity": "warning", "rule": "slow-resource",
             "detail": f"Медленный ресурс ({e.get('duration')}мс): {e.get('name', '')[:80]}"}
            for e in (entries or [])
        ]
    except Exception as e:
        LOG.debug("slow-resource check: %s", e)
        return []


def _check_large_resources(page: Page) -> List[Dict]:
    """Тяжёлые ресурсы (> 2 МБ)."""
    try:
        entries = page.evaluate(f"""() => {{
            return performance.getEntriesByType('resource')
                .filter(e => e.transferSize > {LARGE_RESOURCE_KB * 1024})
                .map(e => ({{ name: e.name.slice(0, 120), size: Math.round(e.transferSize / 1024), type: e.initiatorType }}))
                .slice(0, 10);
        }}""")
        return [
            {"type": "performance", "severity": "warning", "rule": "large-resource",
             "detail": f"Тяжёлый ресурс ({e.get('size')}КБ): {e.get('name', '')[:80]}"}
            for e in (entries or [])
        ]
    except Exception as e:
        LOG.debug("large-resource check: %s", e)
        return []


def _check_memory_usage(page: Page) -> List[Dict]:
    """Использование памяти (если доступно)."""
    try:
        mem = page.evaluate("""() => {
            if (performance.memory) {
                return {
                    usedJSHeapSize: Math.round(performance.memory.usedJSHeapSize / 1024 / 1024),
                    totalJSHeapSize: Math.round(performance.memory.totalJSHeapSize / 1024 / 1024),
                    jsHeapSizeLimit: Math.round(performance.memory.jsHeapSizeLimit / 1024 / 1024),
                };
            }
            return null;
        }""")
        if mem and mem.get("usedJSHeapSize", 0) > 200:
            return [{
                "type": "performance", "severity": "warning", "rule": "high-memory",
                "detail": f"Высокое потребление памяти: {mem['usedJSHeapSize']}МБ (лимит {mem.get('jsHeapSizeLimit', '?')}МБ)",
            }]
        return []
    except Exception as e:
        LOG.debug("memory check: %s", e)
        return []


def format_performance_issues(issues: List[Dict]) -> str:
    if not issues:
        return ""
    lines = [f"Performance: найдено {len(issues)} проблем(ы):"]
    for i in issues[:10]:
        lines.append(f"  [{i.get('severity', '?').upper()}] {i.get('rule', '?')}: {i.get('detail', '')[:100]}")
    return "\n".join(lines)
