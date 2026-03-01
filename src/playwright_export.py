"""
Генерация Playwright (Python) скрипта из лога шагов сессии.
Используется при PLAYWRIGHT_EXPORT_PATH в config.
"""
from typing import List, Dict, Any, Optional


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def build_playwright_script(
    step_log: List[Dict[str, Any]],
    start_url: str = "",
) -> str:
    """
    По логу шагов сформировать .py скрипт для Playwright.
    Действия: goto, click, fill, select_option, press_key, scroll.
    """
    lines = [
        '"""',
        "Сгенерировано Kventin из лога сессии.",
        "Запуск: playwright run script.py (или python script.py с sync_api).",
        '"""',
        "from playwright.sync_api import sync_playwright",
        "",
        "def main():",
        "    with sync_playwright() as p:",
        "        browser = p.chromium.launch(headless=False)",
        "        page = browser.new_page(viewport={'width': 1920, 'height': 1080})",
        "",
    ]
    if start_url:
        lines.append(f'        page.goto("{_esc(start_url)}", wait_until="domcontentloaded", timeout=30000)')
        lines.append("")

    last_url: Optional[str] = None
    for e in step_log:
        step = e.get("step", 0)
        url = (e.get("url") or "").strip()
        action = (e.get("action") or "").strip().lower()
        selector = (e.get("selector") or "").strip()
        value = (e.get("value") or "").strip()

        lines.append(f"        # Step {step}: {action}")

        if url and url != last_url:
            lines.append(f'        page.goto("{_esc(url[:500])}", wait_until="domcontentloaded", timeout=15000)')
            last_url = url

        if action == "click":
            sel = selector or "unknown"
            if sel.startswith("ref:"):
                lines.append(f'        page.locator("[data-kventin-ref=\\"{}\"]").first.click()  # was {sel}'.format(sel.replace("ref:", "")))
            else:
                lines.append(f'        page.locator("{_esc(sel[:200])}").first.click()')
        elif action == "type" or action == "fill_form":
            sel = selector or "unknown"
            if sel.startswith("ref:"):
                lines.append(f'        page.locator("[data-kventin-ref=\\"{}\"]").first.fill("{_esc(value[:200])}")'.format(sel.replace("ref:", "")))
            else:
                lines.append(f'        page.locator("{_esc(sel[:200])}").first.fill("{_esc(value[:200])}")')
        elif action == "select_option":
            sel = selector or "unknown"
            lines.append(f'        page.locator("{_esc(sel[:200])}").first.select_option(value="{_esc(value[:100])}")')
        elif action == "press_key":
            key = value or "Escape"
            lines.append(f'        page.keyboard.press("{_esc(key[:20])}")')
        elif action == "scroll":
            lines.append("        page.mouse.wheel(0, 600)")
        else:
            lines.append(f"        # action {action} not emitted (selector={selector[:50]})")

        lines.append("")

    lines.append("        browser.close()")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    main()")
    return "\n".join(lines)
