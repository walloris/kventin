"""
Accessibility (a11y) проверки: ARIA, labels, alt, tab-order, контраст.
Запускается периодически и находит проблемы доступности.
"""
import logging
from typing import List, Dict, Tuple, Any

from playwright.sync_api import Page

LOG = logging.getLogger("A11y")


def check_accessibility(page: Page) -> List[Dict[str, Any]]:
    """
    Запустить все a11y проверки. Возвращает список issue:
    [{"type": "a11y", "severity": "warning|error", "rule": "...", "detail": "...", "selector": "..."}]
    """
    issues = []
    issues.extend(_check_images_without_alt(page))
    issues.extend(_check_buttons_without_label(page))
    issues.extend(_check_inputs_without_label(page))
    issues.extend(_check_links_without_text(page))
    issues.extend(_check_heading_hierarchy(page))
    issues.extend(_check_focus_indicators(page))
    issues.extend(_check_color_contrast(page))
    return issues


def _check_images_without_alt(page: Page) -> List[Dict]:
    """Изображения без alt-текста."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            document.querySelectorAll('img').forEach(img => {
                if (img.width < 5 || img.height < 5) return;
                const s = getComputedStyle(img);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                const alt = (img.getAttribute('alt') || '').trim();
                const role = img.getAttribute('role');
                if (!alt && role !== 'presentation' && role !== 'none') {
                    issues.push({
                        selector: img.id ? '#' + img.id : (img.src || '').slice(0, 80),
                        src: (img.src || '').slice(0, 100),
                    });
                }
            });
            return issues.slice(0, 10);
        }""")
        return [
            {"type": "a11y", "severity": "warning", "rule": "img-alt",
             "detail": f"Изображение без alt: {i.get('src', '')[:60]}", "selector": i.get("selector", "")}
            for i in (result or [])
        ]
    except Exception as e:
        LOG.debug("img-alt check: %s", e)
        return []


def _check_buttons_without_label(page: Page) -> List[Dict]:
    """Кнопки без текста и без aria-label."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            document.querySelectorAll('button, [role="button"]').forEach(btn => {
                const s = getComputedStyle(btn);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                if (btn.id && btn.id.startsWith('agent-')) return;
                const text = (btn.textContent || '').trim();
                const label = btn.getAttribute('aria-label') || '';
                const title = btn.getAttribute('title') || '';
                if (!text && !label.trim() && !title.trim()) {
                    issues.push({
                        selector: btn.id ? '#' + btn.id : (btn.className || '').toString().slice(0, 60),
                        html: btn.outerHTML.slice(0, 100),
                    });
                }
            });
            return issues.slice(0, 10);
        }""")
        return [
            {"type": "a11y", "severity": "error", "rule": "button-label",
             "detail": f"Кнопка без текста/aria-label: {i.get('html', '')[:60]}", "selector": i.get("selector", "")}
            for i in (result or [])
        ]
    except Exception as e:
        LOG.debug("button-label check: %s", e)
        return []


def _check_inputs_without_label(page: Page) -> List[Dict]:
    """Input/textarea/select без связанного label или aria-label."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select').forEach(inp => {
                const s = getComputedStyle(inp);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                if (inp.id && inp.id.startsWith('agent-')) return;
                const ariaLabel = (inp.getAttribute('aria-label') || '').trim();
                const ariaLabelledBy = (inp.getAttribute('aria-labelledby') || '').trim();
                const placeholder = (inp.placeholder || '').trim();
                const title = (inp.getAttribute('title') || '').trim();
                let hasLabel = false;
                if (inp.id) {
                    hasLabel = document.querySelector('label[for="' + inp.id + '"]') !== null;
                }
                if (!hasLabel) {
                    let parent = inp.parentElement;
                    for (let i = 0; i < 3 && parent; i++) {
                        if (parent.tagName === 'LABEL') { hasLabel = true; break; }
                        parent = parent.parentElement;
                    }
                }
                if (!hasLabel && !ariaLabel && !ariaLabelledBy && !title) {
                    issues.push({
                        selector: inp.name || inp.id || placeholder || inp.type,
                        type: inp.type || 'text',
                        placeholder: placeholder,
                    });
                }
            });
            return issues.slice(0, 10);
        }""")
        return [
            {"type": "a11y", "severity": "warning", "rule": "input-label",
             "detail": f"Поле [{i.get('type')}] без label/aria-label (placeholder: {i.get('placeholder', '—')[:30]})",
             "selector": i.get("selector", "")}
            for i in (result or [])
        ]
    except Exception as e:
        LOG.debug("input-label check: %s", e)
        return []


def _check_links_without_text(page: Page) -> List[Dict]:
    """Ссылки без текста (пустой a[href])."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const s = getComputedStyle(a);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                const text = (a.textContent || '').trim();
                const ariaLabel = (a.getAttribute('aria-label') || '').trim();
                const title = (a.getAttribute('title') || '').trim();
                if (!text && !ariaLabel && !title) {
                    const img = a.querySelector('img[alt]');
                    if (img && img.getAttribute('alt').trim()) return;
                    issues.push({
                        href: (a.getAttribute('href') || '').slice(0, 80),
                        html: a.outerHTML.slice(0, 100),
                    });
                }
            });
            return issues.slice(0, 10);
        }""")
        return [
            {"type": "a11y", "severity": "warning", "rule": "link-text",
             "detail": f"Ссылка без текста: {i.get('href', '')[:50]}", "selector": i.get("href", "")}
            for i in (result or [])
        ]
    except Exception as e:
        LOG.debug("link-text check: %s", e)
        return []


def _check_heading_hierarchy(page: Page) -> List[Dict]:
    """Проверка иерархии заголовков h1-h6: не должно быть пропусков уровней."""
    try:
        levels = page.evaluate("""() => {
            const headings = [];
            document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => {
                const s = getComputedStyle(h);
                if (s.display !== 'none') headings.push(parseInt(h.tagName[1]));
            });
            return headings;
        }""")
        issues = []
        if not levels:
            return [{"type": "a11y", "severity": "warning", "rule": "heading-hierarchy",
                      "detail": "На странице нет заголовков (h1-h6)", "selector": ""}]
        if levels[0] != 1:
            issues.append({"type": "a11y", "severity": "warning", "rule": "heading-hierarchy",
                           "detail": f"Первый заголовок h{levels[0]}, ожидается h1", "selector": ""})
        for i in range(1, len(levels)):
            if levels[i] > levels[i - 1] + 1:
                issues.append({"type": "a11y", "severity": "warning", "rule": "heading-hierarchy",
                               "detail": f"Пропуск уровня: h{levels[i-1]} → h{levels[i]}", "selector": ""})
                break
        return issues[:5]
    except Exception as e:
        LOG.debug("heading check: %s", e)
        return []


def _check_focus_indicators(page: Page) -> List[Dict]:
    """Проверка: кнопки и ссылки имеют видимый фокус-индикатор при Tab."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            const els = document.querySelectorAll('button, a[href], input, select, textarea, [tabindex]');
            let count = 0;
            for (const el of els) {
                if (count >= 5) break;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                el.focus();
                const focused = getComputedStyle(el);
                const outline = focused.outline || focused.outlineStyle;
                const boxShadow = focused.boxShadow;
                const hasFocus = (outline && outline !== 'none' && !outline.includes('0px'))
                    || (boxShadow && boxShadow !== 'none');
                if (!hasFocus) {
                    issues.push({
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().slice(0, 40),
                    });
                }
                count++;
            }
            document.activeElement?.blur();
            return issues;
        }""")
        return [
            {"type": "a11y", "severity": "warning", "rule": "focus-indicator",
             "detail": f"Нет видимого фокуса: <{i.get('tag')}> {i.get('text', '')[:30]}", "selector": ""}
            for i in (result or [])[:3]
        ]
    except Exception as e:
        LOG.debug("focus check: %s", e)
        return []


def _check_color_contrast(page: Page) -> List[Dict]:
    """Базовая проверка контраста текста (крупные проблемы: белый на белом и т.п.)."""
    try:
        result = page.evaluate("""() => {
            const issues = [];
            const luminance = (r, g, b) => {
                const [rs, gs, bs] = [r, g, b].map(c => {
                    c = c / 255;
                    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
            };
            const parseColor = (str) => {
                const m = str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                return m ? [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])] : null;
            };
            const els = document.querySelectorAll('p, span, a, button, h1, h2, h3, h4, label, li, td');
            let checked = 0;
            for (const el of els) {
                if (checked >= 20) break;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                const text = (el.textContent || '').trim();
                if (!text || text.length < 2) continue;
                const fg = parseColor(s.color);
                const bg = parseColor(s.backgroundColor);
                if (!fg || !bg) continue;
                if (bg[0] === 0 && bg[1] === 0 && bg[2] === 0 && s.backgroundColor.includes('0)')) continue;
                const l1 = luminance(...fg);
                const l2 = luminance(...bg);
                const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
                if (ratio < 3) {
                    issues.push({
                        text: text.slice(0, 40),
                        ratio: ratio.toFixed(1),
                        fg: s.color,
                        bg: s.backgroundColor,
                    });
                }
                checked++;
            }
            return issues.slice(0, 5);
        }""")
        return [
            {"type": "a11y", "severity": "warning", "rule": "color-contrast",
             "detail": f"Низкий контраст ({i.get('ratio')}:1): «{i.get('text', '')[:25]}» fg={i.get('fg')} bg={i.get('bg')}",
             "selector": ""}
            for i in (result or [])
        ]
    except Exception as e:
        LOG.debug("contrast check: %s", e)
        return []


def format_a11y_issues(issues: List[Dict]) -> str:
    """Форматировать a11y-issue в текст для GigaChat / отчёта."""
    if not issues:
        return ""
    lines = [f"Accessibility проверка: найдено {len(issues)} проблем(ы):"]
    for i, issue in enumerate(issues[:15], 1):
        sev = "ERROR" if issue.get("severity") == "error" else "WARN"
        lines.append(f"  [{sev}] {issue.get('rule', '?')}: {issue.get('detail', '')[:100]}")
    return "\n".join(lines)
