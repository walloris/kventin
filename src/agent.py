"""
Основной цикл агента-тестировщика:
ходит по заданной странице, анализирует консоль/сеть/DOM,
советуется с GigaChat, создаёт дефекты в Jira (игнорируя флаки),
держится в пределах переданной страницы (при переходе по ссылке — проверка и возврат).
Все действия видимы: курсор, подсветка элементов.
"""
import re
import time
from playwright.sync_api import sync_playwright, Page

from config import (
    START_URL,
    BROWSER_SLOW_MO,
    HEADLESS,
)
from src.gigachat_client import consult_agent
from src.jira_client import create_jira_issue
from src.page_analyzer import build_context, get_dom_summary
from src.visible_actions import inject_cursor, move_cursor_to, highlight_and_click, safe_highlight


def _same_page(start_url: str, current_url: str) -> bool:
    """
    Проверка: мы всё ещё на переданной странице.
    Если перешли по ссылке (URL изменился) — считаем, что ушли; нужно вернуться.
    Нормализуем URL (без trailing slash и фрагмента) для сравнения.
    """
    def norm(u):
        u = (u or "").split("#")[0].rstrip("/")
        return u.lower()
    return norm(current_url) == norm(start_url)


def run_agent(start_url: str = None):
    """
    Запуск агента. start_url — страница для тестирования.
    Работает бесконечно: анализирует страницу, советуется с GigaChat, выполняет действия,
    при переходе по ссылке на другой сайт — проверяет открытие и возвращается назад.
    """
    start_url = start_url or START_URL
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    console_log = []
    network_failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=BROWSER_SLOW_MO,
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Сбор консоли
        def on_console(msg):
            console_log.append({
                "type": msg.type,
                "text": msg.text,
            })

        page.on("console", on_console)
        page._agent_console_log = console_log

        # Сбор неуспешных ответов сети
        def on_response(response):
            if not response.ok and response.url:
                try:
                    request = response.request
                    network_failures.append({
                        "url": response.url,
                        "status": response.status,
                        "method": request.method,
                    })
                except Exception:
                    pass

        page.on("response", on_response)
        page._agent_network_failures = network_failures

        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1)
            inject_cursor(page)
        except Exception as e:
            print(f"[Agent] Ошибка загрузки {start_url}: {e}")
            browser.close()
            return

        iteration = 0
        while True:
            iteration += 1
            current_url = page.url

            # Ушли по ссылке на другую страницу — проверяем, что открылось, и возвращаемся
            if not _same_page(start_url, current_url):
                print(f"[Agent] Переход по ссылке: {current_url}. Проверка загрузки и возврат на {start_url}")
                try:
                    page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(0.5)
                    inject_cursor(page)
                except Exception as e:
                    print(f"[Agent] Ошибка возврата: {e}")
                continue

            # Очистка старых записей (оставляем последние)
            if len(console_log) > 100:
                del console_log[:-100]
            if len(network_failures) > 50:
                del network_failures[:-50]

            context_str = build_context(page, current_url, console_log, network_failures)
            dom_summary = get_dom_summary(page)

            # Вопрос к GigaChat: что делать дальше?
            question = (
                "Дай один конкретный совет: 1) Какой кликабельный элемент на странице лучше нажать следующим "
                "(укажи текст кнопки/ссылки или селектор), или 2) Есть ли реальный дефект для создания в Jira "
                "(не 404, не флак). Если дефект — напиши кратко: summary и description для тикета. "
                "Если ничего делать не нужно — напиши: СТОП."
            )
            answer = consult_agent(context_str, question)

            if not answer:
                print("[Agent] GigaChat недоступен — пауза 10 сек и повтор.")
                time.sleep(10)
                continue

            answer_upper = answer.strip().upper()
            if "СТОП" in answer_upper and len(answer.strip()) < 50:
                print("[Agent] GigaChat сказал СТОП. Пауза 15 сек и повтор анализа.")
                time.sleep(15)
                continue

            # Попытка распознать: дефект или клик
            if "дефект" in answer.lower() or "тикет" in answer.lower() or "jira" in answer.lower():
                # Парсим summary/description из ответа (упрощённо)
                lines = [s.strip() for s in answer.split("\n") if s.strip()]
                summary = lines[0][:255] if lines else "Дефект (автотест)"
                description = answer[:3000]
                create_jira_issue(summary=summary, description=description)
                time.sleep(5)
                continue

            # Ищем элемент для клика по тексту
            try:
                # Убираем кавычки и лишнее из ответа для поиска по тексту
                possible_text = re.sub(r'^.*?(?:кликни|нажми|нажать|кнопк[ау]|ссылка|элемент)[:\s]+', '', answer, flags=re.I).strip()
                possible_text = re.sub(r'["\'].*["\']', '', possible_text).strip()
                if len(possible_text) > 80:
                    possible_text = possible_text[:80]

                clicked = False
                if possible_text:
                    # Попробовать по тексту кнопки/ссылки
                    for selector in [
                        f'button:has-text("{possible_text}")',
                        f'[role="button"]:has-text("{possible_text}")',
                        f'a:has-text("{possible_text}")',
                        f'input[type="submit"]:has-text("{possible_text}")',
                        f'*:has-text("{possible_text}")',
                    ]:
                        try:
                            loc = page.locator(selector).first
                            if loc.count() > 0:
                                safe_highlight(loc, page, 0.5)
                                highlight_and_click(loc, page)
                                clicked = True
                                print(f"[Agent] Клик по: {selector}")
                                break
                        except Exception:
                            continue

                if not clicked:
                    # Случайный безопасный клик по первой видимой ссылке или кнопке на странице
                    for sel in ['a[href^="http"]', 'a[href^="/"]', 'button', '[role="button"]']:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() > 0:
                                safe_highlight(loc, page, 0.3)
                                highlight_and_click(loc, page)
                                print(f"[Agent] Клик по первому элементу: {sel}")
                                break
                        except Exception:
                            continue
            except Exception as e:
                print(f"[Agent] Ошибка при клике: {e}")

            time.sleep(2)

        browser.close()
