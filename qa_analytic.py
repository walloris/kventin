import inspect
import logging
import sys
import warnings
import time
import json
from io import BytesIO
import html
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import numpy as np
import urllib3
import base64
from jira import JIRA

try:
    from jira.exceptions import JIRAError
except ImportError:
    JIRAError = None  # type: ignore[misc,assignment]

from atlassian import Confluence
from datetime import datetime, timedelta
from typing import Any

# --- ИМПОРТ КОНФИГА ---
def _load_config():
    try:
        import config as user_config_mod
    except ImportError:
        sys.exit(
            "Не найден config.py. Создайте модуль config с ключами "
            "{'jira': {'token': '...'}, 'confluence': {'token': '...'}}."
        )
    cfg = getattr(user_config_mod, "config", None)
    if not isinstance(cfg, dict):
        sys.exit("config.config должен быть словарём с ключами jira и confluence.")
    jt = (cfg.get("jira") or {}).get("token") or ""
    ct = (cfg.get("confluence") or {}).get("token") or ""
    if not str(jt).strip() or not str(ct).strip():
        sys.exit("Задайте непустые токены config['jira']['token'] и config['confluence']['token'].")
    return cfg

config = _load_config()

# Отключаем SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings('ignore')

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
JIRA_URL = "https://jira.sberbank.ru"
JIRA_TOKEN = config['jira']['token']
CONFLUENCE_URL = "https://confluence.sberbank.ru"
CONFLUENCE_TOKEN = config['confluence']['token']
PAGE_ID = "21635596824"

FIXED_START_DATE = "2025-10-28"

MAX_WORKERS = 4
REQUEST_DELAY = 1.0
MAX_RETRIES = 5
BATCH_SIZE = 50
INCREMENTAL_WEEKS = 8
SPECIAL_ACTIVITY_ISSUE = "HRC-3630"
STATE_START = "QA_ANALYTICS_STATE_V1_START"
STATE_END = "QA_ANALYTICS_STATE_V1_END"
AVG_TABLE_SORT_METRIC = "total_avg"  # total_avg | median_week | ft_avg | rg_avg | act_avg | regression_share
TOP_HIGHLIGHT_COUNT = 3
ANOMALY_GROWTH_THRESHOLD_PCT = 40
TEAM_ALERT_HOURS = 30
STATE_PROPERTY_KEY = "qa-analytics-state-v1"
FULL_RECONCILE_EVERY_N_RUNS = 10
ENABLE_CONTENT_PROPERTY_STATE = True

_REGRESS_KE_IDS_RAW = (
    2298599, 3589425, 3304476, 3303802, 3191860, 2288712, 2257858, 2935717,
    3521872, 6355438, 5452084, 5452083, 5452082, 5452085, 3303802, 3304476,
    7993288, 2257817, 2644268, 2298078, 5366436, 2503797, 3930742, 2836020,
    3173847, 2295205, 9643400, 9644020, 9644025, 9644023, 9643401, 9643400, 8553253
)
REGRESS_KE_IDS = tuple(dict.fromkeys(_REGRESS_KE_IDS_RAW))

ALLOWED_TESTERS = {
    "Абдуллаев Магомедэмин Шамильевич",
    "Годунова Дарья Алексеевна", "Гулиев Руслан Иса оглы",
    "Калашников Андрей Романович", "Канаев Леонид Олегович",
    "Меркуленков Дмитрий Игоревич",
    "Метляев Игорь Андреевич", "Петрунин Никита Анатольевич",
    "Приколотина Евгения Александровна",
    "Симиник Даниил Григорьевич", "Синица Захар Алексеевич",
    "Чиж Мария Михайловна"
}

TEAM_BY_LASTNAME = {
    "приколотина": "Perftracker",
    "чиж": "Perftracker",
    "метляев": "CoreTech",
    "абдуллаев": "Core UI 2.0",
    "калашников": "Core UI 2.0",
    "петрунин": "Core UI",
    "годунова": "Core UI",
    "канаев": "Профиль",
    "меркуленков": "Профиль",
    "синица": "ЛюдиСбера",
    "симиник": "Поиск",
}

_ALLOWED_FULL_NORM = set()
_ALLOWED_FIRST_TWO = set()
for _a in ALLOWED_TESTERS:
    _tok = tuple(_a.lower().split())
    _ALLOWED_FULL_NORM.add(" ".join(_tok))
    if len(_tok) >= 2:
        _ALLOWED_FIRST_TWO.add((_tok[0], _tok[1]))

_EMPTY_WEEK = {"features": 0, "regression": 0, "activities": 0}

print_lock = Lock()
request_time_lock = Lock()
last_request_time = 0

# --- КЛИЕНТЫ ---
jira_client = JIRA(server=JIRA_URL, token_auth=JIRA_TOKEN, options={'verify': False})
confluence_client = Confluence(url=CONFLUENCE_URL, token=CONFLUENCE_TOKEN, verify_ssl=False)

def thread_safe_log_info(message):
    with print_lock:
        logger.info("%s", message)

def _is_rate_limit_error(exc):
    if JIRAError is not None and isinstance(exc, JIRAError):
        return getattr(exc, "status_code", None) == 429
    return "429" in str(exc)

def _is_retryable_server_error(exc):
    if JIRAError is not None and isinstance(exc, JIRAError):
        return getattr(exc, "status_code", None) in (502, 503, 504)
    return False


def _is_unauthorized_error(exc):
    if JIRAError is not None and isinstance(exc, JIRAError):
        return getattr(exc, "status_code", None) in (401, 403)
    txt = str(exc).lower()
    return "401" in txt or "403" in txt or "unauthorized" in txt


def _reset_jira_session_cookies():
    """Сбрасывает протухшие cookies Jira-сессии, не трогая токен."""
    try:
        session = getattr(jira_client, "_session", None)
        if session is not None and hasattr(session, "cookies"):
            session.cookies.clear()
            logger.warning("Jira cookies очищены, повторяем запрос с token_auth")
            return True
    except Exception:
        logger.exception("Не удалось очистить cookies Jira-сессии")
    return False


def rate_limited_request(func, *args, **kwargs):
    """Один активный запрос к API за раз; пауза REQUEST_DELAY между успешными вызовами."""
    global last_request_time
    last_err = None
    unauthorized_retried = False
    for attempt in range(MAX_RETRIES):
        try:
            with request_time_lock:
                current_time = time.time()
                if current_time - last_request_time < REQUEST_DELAY:
                    time.sleep(REQUEST_DELAY - (current_time - last_request_time))
                result = func(*args, **kwargs)
                last_request_time = time.time()
            return result
        except Exception as e:
            last_err = e
            if "SSL" in str(e):
                raise
            if _is_unauthorized_error(e) and not unauthorized_retried:
                unauthorized_retried = True
                if _reset_jira_session_cookies():
                    time.sleep(1)
                    continue
            if _is_rate_limit_error(e):
                logger.warning("Лимит запросов (429), пауза 60 с (попытка %s/%s)", attempt + 1, MAX_RETRIES)
                time.sleep(60)
                continue
            if _is_retryable_server_error(e):
                wait = 5 * (attempt + 1)
                logger.warning(
                    "Ошибка сервера Jira, повтор через %s с (попытка %s/%s): %s",
                    wait, attempt + 1, MAX_RETRIES, e,
                )
                time.sleep(wait)
                continue
            if _is_unauthorized_error(e):
                logger.error("Jira: 401/403 после повторной аутентификации. Проверьте token/cookie/SSO: %s", e)
                raise
            logger.exception("Запрос не выполнен (попытка %s/%s)", attempt + 1, MAX_RETRIES)
            raise
    raise RuntimeError("Запрос не удался после всех повторов") from last_err

def seconds_to_hours(seconds):
    return round(seconds / 3600, 2)

def is_allowed_tester(name):
    if not name or name == "Неизвестно":
        return False
    nt = tuple(name.lower().split())
    if not nt:
        return False
    if " ".join(nt) in _ALLOWED_FULL_NORM:
        return True
    if len(nt) >= 2 and (nt[0], nt[1]) in _ALLOWED_FIRST_TWO:
        return True
    return False

def week_bucket(author_weeks, week):
    """Читает неделю без создания ключа во вложенном defaultdict."""
    d = author_weeks.get(week)
    return d if d is not None else _EMPTY_WEEK

def calendar_week_monday_key(ref=None):
    """Понедельник ISO-недели (0=Пн), строка YYYY-MM-DD."""
    ref = ref or datetime.now()
    return get_week_start(ref.strftime("%Y-%m-%d"))

def week_has_any_activity(unified_data, week):
    return any(
        week_bucket(unified_data[auth], week)["features"] > 0
        or week_bucket(unified_data[auth], week)["regression"] > 0
        or week_bucket(unified_data[auth], week)["activities"] > 0
        for auth in unified_data
    )


def create_data_store():
    unified = defaultdict(lambda: defaultdict(lambda: {"features": 0, "regression": 0, "activities": 0}))
    by_project = defaultdict(lambda: {"times": [], "stories": []})
    author_stats = defaultdict(lambda: defaultdict(int))
    return unified, by_project, author_stats


def nested_to_dict(obj):
    if isinstance(obj, defaultdict):
        obj = dict(obj)
    if isinstance(obj, dict):
        return {k: nested_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [nested_to_dict(v) for v in obj]
    return obj


def fetch_confluence_page(page_id, expand="version,space,title,ancestors,body.storage"):
    page_id_str = str(page_id)
    path = "rest/api/content/{0}".format(page_id_str)
    if hasattr(confluence_client, "get_page_by_id"):
        return confluence_client.get_page_by_id(page_id_str, expand=expand)
    return confluence_client.get(path, params={"expand": expand})


def get_content_property(page_id, key):
    candidates = [
        f"rest/api/content/{page_id}/property/{key}",
        f"rest/api/latest/content/{page_id}/property/{key}",
    ]
    for path in candidates:
        try:
            return confluence_client.get(path)
        except Exception:
            continue
    return None


def set_content_property(page_id, key, value):
    update_candidates = [
        f"rest/api/content/{page_id}/property/{key}",
        f"rest/api/latest/content/{page_id}/property/{key}",
    ]
    create_candidates = [
        f"rest/api/content/{page_id}/property",
        f"rest/api/latest/content/{page_id}/property",
    ]

    def _send_json(method_name, path, payload):
        method = getattr(confluence_client, method_name)
        try:
            return method(path, json=payload)
        except TypeError:
            return method(path, data=payload)

    existing = get_content_property(page_id, key)
    if existing and "id" in existing:
        payload = {
            "id": existing["id"],
            "key": key,
            "value": value,
            "version": {"number": existing["version"]["number"] + 1},
        }
        last_err = None
        for path in update_candidates:
            try:
                return _send_json("put", path, payload)
            except Exception as e:
                last_err = e
        if last_err is not None:
            raise last_err

    payload = {"key": key, "value": value}
    last_err = None
    for path in create_candidates:
        try:
            return _send_json("post", path, payload)
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise last_err


def save_state_to_confluence(page_id, key, state_payload, report_html):
    """Пытается сохранить state в content property; при несовместимости тихо уходит в fallback страницы."""
    if not ENABLE_CONTENT_PROPERTY_STATE:
        fallback_html = report_html + render_page_state(state_payload)
        update_confluence_manual(page_id, fallback_html)
        return "page_fallback"
    try:
        set_content_property(page_id, key, state_payload)
        logger.info("Состояние отчёта сохранено в content property %s", key)
        return "content_property"
    except Exception as e:
        logger.warning("Content property недоступен (%s). Используем fallback в странице.", e)
        fallback_html = report_html + render_page_state(state_payload)
        update_confluence_manual(page_id, fallback_html)
        return "page_fallback"


def extract_page_state(storage_body):
    if not storage_body:
        return None
    start_idx = storage_body.find(STATE_START)
    end_idx = storage_body.find(STATE_END)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    raw = storage_body[start_idx + len(STATE_START):end_idx].strip()
    try:
        return json.loads(html.unescape(raw))
    except Exception:
        logger.exception("Не удалось разобрать служебное состояние из Confluence")
        return None


def load_page_state(page_id):
    try:
        prop = get_content_property(page_id, STATE_PROPERTY_KEY)
        if prop and "value" in prop:
            return prop["value"]
        page = fetch_confluence_page(page_id)
        storage = (page.get("body", {}).get("storage", {}) or {}).get("value", "")
        return extract_page_state(storage)
    except Exception:
        logger.exception("Не удалось прочитать состояние из Confluence")
        return None


def build_page_state(unified_data, by_project_data, author_project_stats):
    return {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "confluence-page-storage",
        "schema_version": 2,
        "weekly_data": nested_to_dict(unified_data),
        "by_project": nested_to_dict(by_project_data),
        "author_project_stats": nested_to_dict(author_project_stats),
    }


def render_page_state(state_payload):
    raw = json.dumps(state_payload, ensure_ascii=False, separators=(",", ":"))
    escaped_raw = html.escape(raw)
    return f"""
    <ac:structured-macro ac:name="expand">
      <ac:parameter ac:name="title">Служебное состояние отчёта</ac:parameter>
      <ac:rich-text-body>
        <p>Не редактировать вручную: этот блок нужен Jenkins-джобе для инкрементального обновления.</p>
        <pre>{STATE_START}
{escaped_raw}
{STATE_END}</pre>
      </ac:rich-text-body>
    </ac:structured-macro>
    """


def inject_cache_history(cache_payload, unified_data, by_project_data, author_project_stats, recalc_start):
    if not cache_payload:
        return
    old_weekly = cache_payload.get("weekly_data", {})
    for author, weeks in old_weekly.items():
        for week, metrics in weeks.items():
            if week >= recalc_start:
                continue
            dst = unified_data[author][week]
            dst["features"] = metrics.get("features", 0)
            dst["regression"] = metrics.get("regression", 0)
            dst["activities"] = metrics.get("activities", 0)

    def story_before_recalc(story):
        wk_field = str(story.get("worklog_weeks", "")).strip()
        if wk_field:
            weeks_list = [w.strip() for w in wk_field.split(",") if w.strip()]
            if not weeks_list:
                return True
            return max(weeks_list) < recalc_start
        dates = str(story.get("worklog_dates", "")).split(",")
        dates = [d.strip() for d in dates if d.strip() and d.strip() != "Нет данных"]
        # Если даты не разобрались, не выбрасываем задачу из состояния:
        # так мы сохраняем полную историю времени с базовой даты отчёта.
        if not dates:
            return True
        return max(dates) < recalc_start

    old_projects = cache_payload.get("by_project", {})
    for proj, pdata in old_projects.items():
        old_stories = [s for s in pdata.get("stories", []) if story_before_recalc(s)]
        if not old_stories:
            continue
        by_project_data[proj]["stories"].extend(old_stories)
        by_project_data[proj]["times"].extend([s.get("total_time", 0) for s in old_stories])

    # Проектную статистику по авторам пересчитываем на лету из свежей выборки,
    # чтобы в инкрементальном режиме не накапливать дубли по окну пересчёта.


def normalize_recalc_week_start(weeks_back):
    base = datetime.now() - timedelta(weeks=weeks_back)
    monday = base - timedelta(days=base.weekday())
    return monday.strftime("%Y-%m-%d")


def historical_weeks_set(unified_data, before_week):
    result = set()
    for author in unified_data:
        for week in unified_data[author].keys():
            if week < before_week:
                result.add(week)
    return result


def validate_incremental_integrity(prev_state, unified_data, recalc_start_key):
    if not prev_state:
        return True
    prev_weeks = set()
    for _, weeks in (prev_state.get("weekly_data", {}) or {}).items():
        for week in weeks.keys():
            if week < recalc_start_key:
                prev_weeks.add(week)
    now_weeks = historical_weeks_set(unified_data, recalc_start_key)
    missing = prev_weeks - now_weeks
    if missing:
        logger.error("Инвариант нарушен: потерялись исторические недели до окна инкремента: %s", sorted(missing)[:10])
        return False
    return True

def get_week_start(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    start = dt - timedelta(days=dt.weekday())
    return start.strftime('%Y-%m-%d')


def get_full_worklogs_list(issue: object) -> list[Any]:
    """Все worklog-записи задачи; во вложении issue Jira отдаёт только первую порцию."""
    container = getattr(getattr(issue, "fields", None), "worklog", None)
    if container is None:
        return []
    embedded = list(getattr(container, "worklogs", []) or [])
    total = getattr(container, "total", None)
    if total is None:
        total = len(embedded)
    if total > len(embedded):
        key = getattr(issue, "key", None)
        if not key:
            return embedded
        try:
            return list(rate_limited_request(jira_client.worklogs, key))
        except Exception:
            logger.exception("Не удалось загрузить полный worklog для %s", key)
            return embedded
    return embedded


def parse_worklogs_local(worklogs_list, start_date_obj):
    filtered = []
    authors = set()
    for wl in worklogs_list:
        started = getattr(wl, 'started', wl.get('started', '')) if isinstance(wl, dict) else wl.started
        time_spent = getattr(wl, 'timeSpentSeconds', wl.get('timeSpentSeconds', 0)) if isinstance(wl, dict) else wl.timeSpentSeconds
        author_obj = getattr(wl, 'author', wl.get('author', {})) if isinstance(wl, dict) else wl.author
        if hasattr(author_obj, 'displayName'): author_name = author_obj.displayName
        elif isinstance(author_obj, dict): author_name = author_obj.get('displayName', 'Неизвестно')
        else: author_name = 'Неизвестно'

        if not started: continue
        try:
            wl_date = datetime.strptime(started[:10], '%Y-%m-%d')
        except ValueError:
            continue
        if wl_date < start_date_obj: continue
        if not is_allowed_tester(author_name): continue

        filtered.append({'timeSpentSeconds': time_spent, 'started': started, 'author_name': author_name, 'week_start': get_week_start(started[:10])})
        authors.add(author_name)
    return filtered, authors

def _related_issue_keys(issue: object) -> set[str]:
    """Ключи задач, с которых забираем worklog вместе со Story/Bug: issue links + подзадачи."""
    keys: set[str] = set()
    if hasattr(issue.fields, "issuelinks") and issue.fields.issuelinks:
        for link in issue.fields.issuelinks:
            target = getattr(link, "outwardIssue", getattr(link, "inwardIssue", None))
            if target is not None:
                k = getattr(target, "key", None)
                if k:
                    keys.add(k)
    if hasattr(issue.fields, "subtasks") and issue.fields.subtasks:
        for sub in issue.fields.subtasks:
            k = getattr(sub, "key", None)
            if not k and isinstance(sub, dict):
                k = sub.get("key")
            if k:
                keys.add(k)
    return keys


def fetch_linked_tasks_bulk(parent_issues):
    linked_keys: set[str] = set()
    for issue in parent_issues:
        linked_keys.update(_related_issue_keys(issue))
    if not linked_keys:
        return {}
    linked_tasks_map = {}
    linked_keys_list = list(linked_keys)
    for i in range(0, len(linked_keys_list), 50):
        batch_keys = linked_keys_list[i:i+50]
        keys_str = ",".join(batch_keys)
        jql = f"key in ({keys_str})"
        try:
            tasks = rate_limited_request(jira_client.search_issues, jql, maxResults=1000, fields='summary,worklog,issuetype,project')
            for t in tasks: linked_tasks_map[t.key] = t
        except Exception:
            logger.exception("Не загрузился батч связанных задач JQL от %s ключей", len(batch_keys))
    return linked_tasks_map

def analyze_features(start_date_obj, query_start_date, unified_data, by_project_data, author_project_stats):
    logger.info("--- Этап 1: Анализ Feature (Story/Bug) ---")
    jql = f"""
        project in (HRM, HRC, PERFREVIEW, HRPASSIST, SFILE, NEUROUI, SEARCHCS)
        AND issuetype in (Story, Bug)
        AND updated >= "{query_start_date}"
    """
    all_issues = []
    start_at = 0
    while True:
        batch = rate_limited_request(
            jira_client.search_issues,
            jql,
            startAt=start_at,
            maxResults=100,
            fields="summary,priority,customfield_18300,issuelinks,subtasks,worklog,project,issuetype",
        )
        if not batch: break
        all_issues.extend(batch)
        logger.info("  Загружено %s задач...", len(all_issues))
        if len(batch) < 100: break
        start_at += 100

    batches = [all_issues[i:i + BATCH_SIZE] for i in range(0, len(all_issues), BATCH_SIZE)]
    total_ft_time = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_feature_batch, batch, start_date_obj) for batch in batches]
        for future in as_completed(futures):
            for res in future.result():
                for w_stat in res['weekly_stats']:
                    unified_data[w_stat['author']][w_stat['week']]['features'] += w_stat['time']
                for auth, time_spent in res['project_stats'].items():
                    author_project_stats[auth][res['project_key']] += time_spent
                pk = res['project_key']
                by_project_data[pk]['times'].append(res['total_time'])
                by_project_data[pk]['stories'].append(res['story_data'])
                total_ft_time += res['total_time']
    return total_ft_time

def process_feature_batch(issues, start_date_obj):
    results = []
    linked_map = fetch_linked_tasks_bulk(issues)
    for issue in issues:
        res = process_single_feature(issue, start_date_obj, linked_map)
        if res: results.append(res)
    return results

def process_single_feature(issue, start_date_obj, linked_map):
    try:
        project_key = issue.fields.project.key
        story_wls = get_full_worklogs_list(issue)
        valid_story_wls, _ = parse_worklogs_local(story_wls, start_date_obj)
        story_time = sum(w['timeSpentSeconds'] for w in valid_story_wls)
        linked_time, linked_tasks_info, weekly_stats, all_dates, all_authors, project_stats = 0, [], [], set(), set(), defaultdict(int)
        for wl in valid_story_wls:
            weekly_stats.append({'author': wl['author_name'], 'week': wl['week_start'], 'time': wl['timeSpentSeconds']})
            project_stats[wl['author_name']] += wl['timeSpentSeconds']
            all_dates.add(wl['started'][:10])
            all_authors.add(wl['author_name'])
        for rel_key in sorted(_related_issue_keys(issue)):
            if rel_key not in linked_map:
                continue
            task = linked_map[rel_key]
            t_wls = get_full_worklogs_list(task)
            valid_t_wls, _ = parse_worklogs_local(t_wls, start_date_obj)
            t_time = sum(w["timeSpentSeconds"] for w in valid_t_wls)
            if t_time <= 0:
                continue
            linked_time += t_time
            for w in valid_t_wls:
                weekly_stats.append(
                    {"author": w["author_name"], "week": w["week_start"], "time": w["timeSpentSeconds"]}
                )
                project_stats[w["author_name"]] += w["timeSpentSeconds"]
                all_dates.add(w["started"][:10])
                all_authors.add(w["author_name"])
            linked_tasks_info.append({"key": task.key, "time": t_time})
        total_time = story_time + linked_time
        if story_time > 0 and linked_time > 0:
            source = "Story+Linked"
        elif linked_time > 0:
            source = "Linked"
        else:
            source = "Story"
        if total_time == 0: return None
        worklog_weeks_sorted = sorted({stat["week"] for stat in weekly_stats})
        worklog_weeks_str = ", ".join(worklog_weeks_sorted) if worklog_weeks_sorted else ""
        thread_safe_log_info(f"✓ {issue.key} ({project_key}) - {seconds_to_hours(total_time)}ч")
        return {'project_key': project_key, 'total_time': total_time, 'weekly_stats': weekly_stats, 'project_stats': dict(project_stats),
                'story_data': {'key': issue.key, 'issuetype': issue.fields.issuetype.name, 'summary': issue.fields.summary, 'source': source,
                               'total_time': total_time, 'worklog_weeks': worklog_weeks_str,
                               'worklog_dates': ', '.join(sorted(list(all_dates))) if all_dates else 'Нет данных',
                               'authors': list(all_authors), 'linked_tasks': linked_tasks_info, 'ke': str(getattr(issue.fields, 'customfield_18300', "Не указано"))}}
    except Exception:
        logger.exception("Ошибка разбора фичи %s", getattr(issue, "key", "?"))
        return None

def analyze_regression(start_date_obj, query_start_date, unified_data, author_project_stats):
    logger.info("--- Этап 2: Анализ Регресса (HRPRELEASE) ---")
    ke_ids_str = ",".join(map(str, REGRESS_KE_IDS))
    jql = f"project = HRPRELEASE AND type = \"Release 2.0\" AND \"КЭ\" in ({ke_ids_str}) AND updated >= \"{query_start_date}\""
    issues = []
    start_at = 0
    while True:
        batch = rate_limited_request(jira_client.search_issues, jql, startAt=start_at, maxResults=50, fields='summary,subtasks')
        if not batch: break
        issues.extend(batch)
        if len(batch) < 50: break
        start_at += 50
    logger.info("  Найдено %s релизов...", len(issues))
    total_regress_time = 0
    all_subtask_keys = [sub.key for rel in issues if hasattr(rel.fields, 'subtasks') for sub in rel.fields.subtasks]
    if not all_subtask_keys: return 0
    batches = [all_subtask_keys[i:i + 50] for i in range(0, len(all_subtask_keys), 50)]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_regress_subtasks, batch, start_date_obj) for batch in batches]
        for future in as_completed(futures):
            for stat in future.result():
                unified_data[stat['author']][stat['week']]['regression'] += stat['time']
                total_regress_time += stat['time']
    return total_regress_time

def process_regress_subtasks(keys, start_date_obj):
    results = []
    if not keys: return results
    jql = f"key in ({','.join(keys)})"
    try:
        tasks = rate_limited_request(jira_client.search_issues, jql, maxResults=100, fields='summary,worklog,labels')
        for t in tasks:
            is_regress = ('QA_REGRESS_TIME' in getattr(t.fields, 'labels', [])) or ('тестирование' in t.fields.summary.lower())
            if not is_regress: continue
            wls = get_full_worklogs_list(t)
            valid_wls, _ = parse_worklogs_local(wls, start_date_obj)
            for w in valid_wls:
                results.append({'author': w['author_name'], 'week': w['week_start'], 'time': w['timeSpentSeconds']})
    except Exception:
        logger.exception("Ошибка загрузки подзадач регресса (%s ключей)", len(keys))
    return results


def analyze_special_activity(issue_key, start_date_obj, unified_data, by_project_data, author_project_stats):
    logger.info("--- Этап 3: Активности из %s ---", issue_key)
    try:
        issue = rate_limited_request(jira_client.issue, issue_key, fields="summary,worklog,project,issuetype")
    except Exception:
        logger.exception("Не удалось загрузить задачу активностей %s", issue_key)
        return 0

    worklogs = get_full_worklogs_list(issue)
    valid_wls, _ = parse_worklogs_local(worklogs, start_date_obj)
    if not valid_wls:
        return 0

    project_key = issue.fields.project.key if hasattr(issue.fields, "project") else "HRC"
    total = 0
    by_week = defaultdict(int)
    by_author = set()
    for wl in valid_wls:
        sec = wl["timeSpentSeconds"]
        total += sec
        by_week[wl["week_start"]] += sec
        by_author.add(wl["author_name"])
        unified_data[wl["author_name"]][wl["week_start"]]["activities"] += sec
        author_project_stats[wl["author_name"]][project_key] += sec

    by_project_data[project_key]["times"].append(total)
    week_keys_sorted = sorted(by_week.keys())
    by_project_data[project_key]["stories"].append(
        {
            "key": issue.key,
            "issuetype": issue.fields.issuetype.name if hasattr(issue.fields, "issuetype") else "Task",
            "summary": f"{issue.fields.summary} (активности)",
            "source": "Activity",
            "total_time": total,
            "worklog_weeks": ", ".join(week_keys_sorted),
            "worklog_dates": ", ".join(week_keys_sorted),
            "authors": sorted(by_author),
            "linked_tasks": [],
            "ke": "n/a",
        }
    )
    logger.info("  %s: добавлено %s ч активностей", issue.key, seconds_to_hours(total))
    return total

def determine_team(author, author_project_stats):
    lastname = (author or "").strip().lower().split()
    if lastname:
        mapped = TEAM_BY_LASTNAME.get(lastname[0])
        if mapped:
            return mapped
    if author not in author_project_stats or not author_project_stats[author]: return "Вне команды / Релиз"
    stats = author_project_stats[author].copy()
    # Время по HRPRELEASE не относим к «команде» проекта — только релизный поток.
    if 'HRPRELEASE' in stats: del stats['HRPRELEASE']
    if not stats: return "Вне команды / Релиз"
    return max(stats, key=stats.get)


def ordered_report_weeks(unified_data, limit=None, ref_date=None):
    current_week_key = calendar_week_monday_key(ref_date)
    all_weeks = set()
    for auth_data in unified_data.values():
        all_weeks.update(auth_data.keys())
    all_weeks.add(current_week_key)
    ordered = sorted(all_weeks, reverse=True)
    if limit is None:
        return ordered
    return ordered[:limit]


def sum_week_metric(unified_data, week, metric):
    return sum(week_bucket(unified_data[auth], week)[metric] for auth in unified_data)


def sum_author_week(author_weeks, week):
    wb = week_bucket(author_weeks, week)
    return wb["features"] + wb["regression"] + wb["activities"]


def format_delta(current, previous):
    cur_h = seconds_to_hours(current)
    prev_h = seconds_to_hours(previous)
    delta = round(cur_h - prev_h, 2)
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def format_delta_pct(current, previous):
    cur_h = seconds_to_hours(current)
    prev_h = seconds_to_hours(previous)
    if prev_h <= 0:
        return "n/a"
    return f"{round(((cur_h - prev_h) / prev_h) * 100, 1)}%"


def weekly_total_series(unified_data, author):
    weeks = ordered_report_weeks(unified_data, limit=None)
    return [sum_author_week(unified_data[author], w) for w in weeks]


def story_touches_weeks(story: dict[str, Any], weeks: list[str] | set[str]) -> bool:
    """True, если по дате списания (worklog started) есть время в одной из недель отчёта."""
    target_weeks = set(weeks) if not isinstance(weeks, set) else weeks
    wk_field = str(story.get("worklog_weeks", "")).strip()
    if wk_field:
        for raw in wk_field.split(","):
            raw = raw.strip()
            if raw and raw in target_weeks:
                return True
        return False
    dates = str(story.get("worklog_dates", "")).split(",")
    for raw in dates:
        raw = raw.strip()
        if not raw or raw == "Нет данных":
            continue
        try:
            if get_week_start(raw) in target_weeks:
                return True
        except ValueError:
            continue
    return False


def generate_average_by_author_table(unified_data, author_project_stats):
    all_weeks = ordered_report_weeks(unified_data, limit=None)
    week_count = max(len(all_weeks), 1)
    rows = []
    for author in sorted(unified_data.keys()):
        weekly_totals = weekly_total_series(unified_data, author)
        ft = sum(week_bucket(unified_data[author], w)["features"] for w in all_weeks)
        rg = sum(week_bucket(unified_data[author], w)["regression"] for w in all_weeks)
        act = sum(week_bucket(unified_data[author], w)["activities"] for w in all_weeks)
        total = ft + rg + act
        if total == 0:
            continue
        active_weeks = sum(1 for w in all_weeks if sum_author_week(unified_data[author], w) > 0)
        regression_share = round((rg / total) * 100, 1) if total > 0 else 0
        median_week = seconds_to_hours(float(np.median(weekly_totals)))
        std_dev = seconds_to_hours(float(np.std(weekly_totals)))
        stability_idx = round(100 / (1 + std_dev), 1)
        rows.append(
            {
                "author": author,
                "team": determine_team(author, author_project_stats),
                "ft_avg": seconds_to_hours(ft / week_count),
                "rg_avg": seconds_to_hours(rg / week_count),
                "act_avg": seconds_to_hours(act / week_count),
                "total_avg": seconds_to_hours(total / week_count),
                "median_week": median_week,
                "regression_share": regression_share,
                "std_dev": std_dev,
                "stability_idx": stability_idx,
                "active_weeks": active_weeks,
            }
        )

    sort_key = AVG_TABLE_SORT_METRIC if AVG_TABLE_SORT_METRIC in {"total_avg", "median_week", "ft_avg", "rg_avg", "act_avg", "regression_share", "stability_idx"} else "total_avg"
    rows.sort(key=lambda x: x[sort_key], reverse=True)
    top_people = {r["author"] for r in rows[:TOP_HIGHLIGHT_COUNT]}
    bottom_people = {r["author"] for r in rows[-TOP_HIGHLIGHT_COUNT:]} if len(rows) > TOP_HIGHLIGHT_COUNT else set()

    table = "<h3 style='margin:14px 0 8px;'>Среднее время по сотрудникам (за весь период)</h3>"
    table += f"<p style='margin:0 0 8px; color:#6b778c;'>Сортировка: <strong>{html.escape(sort_key)}</strong>. Топ-{TOP_HIGHLIGHT_COUNT} и нижние-{TOP_HIGHLIGHT_COUNT} подсвечены.</p>"
    table += (
        "<table style='border-collapse:collapse; width:100%; margin-bottom:14px;'>"
        "<thead><tr>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7; text-align:left;'>Сотрудник</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7; text-align:left;'>Команда</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Фичи (ср/нед)</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Регресс (ср/нед)</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Активности (ср/нед)</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Итого (ср/нед)</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Медиана (ч/нед)</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Доля регресса, %</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Стабильность, %</th>"
        "<th style='padding:7px; border:1px solid #dfe1e6; background:#f4f5f7;'>Активных недель</th>"
        "</tr></thead><tbody>"
    )
    for r in rows:
        row_bg = ""
        if r["author"] in top_people:
            row_bg = "background:#e3fcef;"
        elif r["author"] in bottom_people:
            row_bg = "background:#ffebe6;"
        reg_share_style = "color:#bf2600; font-weight:600;" if r["regression_share"] >= 40 else ""
        table += (
            f"<tr style='{row_bg}'><td style='padding:7px; border:1px solid #dfe1e6; text-align:left;'>{html.escape(r['author'])}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:left;'>{html.escape(r['team'])}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['ft_avg']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['rg_avg']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['act_avg']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center; font-weight:600;'>{r['total_avg']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['median_week']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center; {reg_share_style}'>{r['regression_share']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['stability_idx']}</td>"
            f"<td style='padding:7px; border:1px solid #dfe1e6; text-align:center;'>{r['active_weeks']}</td></tr>"
        )
    table += "</tbody></table>"
    table += f"<p style='margin:-4px 0 14px; color:#6b778c;'>Период расчёта: {len(all_weeks)} недель (с {FIXED_START_DATE}).</p>"
    return table


def generate_weekly_report(unified_data, author_project_stats, ref_date=None):
    if not unified_data: return ""

    ref_date = ref_date or datetime.now()
    current_week_key = calendar_week_monday_key(ref_date)

    sorted_weeks = ordered_report_weeks(unified_data, limit=None, ref_date=ref_date)

    # Определяем команды
    teams_map = defaultdict(list)
    for author in unified_data.keys():
        team = determine_team(author, author_project_stats)
        teams_map[team].append(author)
    sorted_teams = sorted(teams_map.keys())

    style_table = "border-collapse: collapse; width: 100%; font-family: Arial; font-size: 11px; margin-bottom: 20px;"
    style_th = "background: #f4f5f7; border: 1px solid #ddd; padding: 5px; text-align: center;"
    style_td = "border: 1px solid #ddd; padding: 4px; text-align: center;"
    style_team = "background: #deebff; font-weight: bold; padding: 8px; text-align: left;"

    full_html = "<h3>📊 Нагрузка по неделям (весь период)</h3>"

    for week in sorted_weeks:
        monday = datetime.strptime(week, '%Y-%m-%d')
        friday = monday + timedelta(days=4)
        week_label = f"Неделя: {monday.strftime('%d.%m.%Y')} – {friday.strftime('%d.%m.%Y')}"
        is_calendar_current = week == current_week_key
        title_suffix = " (Текущая)" if is_calendar_current else ""

        # Генерация таблицы для конкретной недели
        week_table = f"<table style='{style_table}'><thead>"
        week_table += f"<tr><th style='{style_th} width: 220px;'>Сотрудник</th>"
        week_table += f"<th style='{style_th}'>Фичи</th><th style='{style_th}'>Регресс</th><th style='{style_th}'>Активности</th><th style='{style_th}'>Сумма</th></tr></thead><tbody>"

        if is_calendar_current and not week_has_any_activity(unified_data, week):
            week_table += (
                f"<tr><td colspan='5' style='{style_td} text-align:left; color:#666;'>"
                "Списаний за текущую календарную неделю в выборке Jira пока нет.</td></tr>"
            )

        # Заполняем данными по командам
        for team in sorted_teams:
            if team == "Вне команды / Релиз" and len(sorted_teams) > 1: continue

            # Проверяем, есть ли активность в этой команде на этой неделе
            team_active = False
            for auth in teams_map[team]:
                wb = week_bucket(unified_data[auth], week)
                if wb['features'] > 0 or wb['regression'] > 0 or wb['activities'] > 0:
                    team_active = True
                    break

            if not team_active: continue

            week_table += f"<tr><td colspan='5' style='{style_team}'>{html.escape(team)}</td></tr>"

            for auth in sorted(teams_map[team]):
                wb = week_bucket(unified_data[auth], week)
                ft = seconds_to_hours(wb['features'])
                reg = seconds_to_hours(wb['regression'])
                act = seconds_to_hours(wb['activities'])

                # Если у сотрудника 0 часов за неделю - пропускаем его, чтобы таблица была чище
                if ft == 0 and reg == 0 and act == 0: continue

                total = round(ft + reg + act, 2)
                st_ft = "color:#ccc;" if ft == 0 else ""
                st_reg = "color:#ccc;" if reg == 0 else "color:#e74c3c;"
                st_act = "color:#ccc;" if act == 0 else "color:#8e44ad;"
                st_tot = "background:#e3fcef; font-weight:bold;" if total > 30 else ("background:#fff;" if total > 0 else "color:#eee;")

                week_table += f"<tr><td style='{style_td} text-align:left;'>{html.escape(auth)}</td>"
                week_table += f"<td style='{style_td} {st_ft}'>{ft if ft > 0 else '-'}</td>"
                week_table += f"<td style='{style_td} {st_reg}'>{reg if reg > 0 else '-'}</td>"
                week_table += f"<td style='{style_td} {st_act}'>{act if act > 0 else '-'}</td>"
                week_table += f"<td style='{style_td} {st_tot}'>{total}</td></tr>"

        # Обработка "Вне команды" отдельно в конце
        if "Вне команды / Релиз" in teams_map:
            team = "Вне команды / Релиз"
            team_active = any(
                (
                    week_bucket(unified_data[auth], week)['features'] > 0
                    or week_bucket(unified_data[auth], week)['regression'] > 0
                    or week_bucket(unified_data[auth], week)['activities'] > 0
                )
                for auth in teams_map[team]
            )

            if team_active:
                week_table += f"<tr><td colspan='5' style='{style_team} background:#f4f5f7; color:#666;'>{html.escape(team)}</td></tr>"
                for auth in sorted(teams_map[team]):
                    wb = week_bucket(unified_data[auth], week)
                    ft = seconds_to_hours(wb['features'])
                    reg = seconds_to_hours(wb['regression'])
                    act = seconds_to_hours(wb['activities'])
                    if ft == 0 and reg == 0 and act == 0: continue
                    total = round(ft + reg + act, 2)
                    week_table += f"<tr><td style='{style_td} text-align:left;'>{html.escape(auth)}</td>"
                    week_table += f"<td style='{style_td}'>{ft if ft>0 else '-'}</td><td style='{style_td}'>{reg if reg>0 else '-'}</td><td style='{style_td}'>{act if act>0 else '-'}</td><td style='{style_td}'>{total}</td></tr>"

        week_table += "</tbody></table>"

        # Раскрыта только календарная «текущая» неделя; остальные — expand.
        week_title_esc = html.escape(f"{week_label}{title_suffix}")
        if is_calendar_current:
            full_html += f"<h4>{week_title_esc}</h4>{week_table}"
        else:
            full_html += f"""
            <ac:structured-macro ac:name="expand">
                <ac:parameter ac:name="title">{week_title_esc}</ac:parameter>
                <ac:rich-text-body>{week_table}</ac:rich-text-body>
            </ac:structured-macro>
            """

    return full_html

def generate_html_report(unified_data, by_project_data, author_project_stats, chart_b64):
    report_weeks = ordered_report_weeks(unified_data, limit=None)
    current_week = calendar_week_monday_key()
    previous_week = get_week_start((datetime.strptime(current_week, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    rolling_weeks = report_weeks[:4]

    current_total = sum_week_metric(unified_data, current_week, "features") + sum_week_metric(unified_data, current_week, "regression") + sum_week_metric(unified_data, current_week, "activities")
    previous_total = sum_week_metric(unified_data, previous_week, "features") + sum_week_metric(unified_data, previous_week, "regression") + sum_week_metric(unified_data, previous_week, "activities")
    current_features = sum_week_metric(unified_data, current_week, "features")
    current_regression = sum_week_metric(unified_data, current_week, "regression")
    current_activities = sum_week_metric(unified_data, current_week, "activities")
    rolling_total = sum(
        sum_week_metric(unified_data, week, "features")
        + sum_week_metric(unified_data, week, "regression")
        + sum_week_metric(unified_data, week, "activities")
        for week in rolling_weeks
    )
    rolling_avg = rolling_total / max(len(rolling_weeks), 1)

    team_map = defaultdict(list)
    for author in unified_data.keys():
        team_map[determine_team(author, author_project_stats)].append(author)

    # Быстрые инсайты для руководителя: кто перегружен, где много регресса.
    author_now = []
    for author in unified_data.keys():
        wb = week_bucket(unified_data[author], current_week)
        total = wb["features"] + wb["regression"] + wb["activities"]
        if total > 0:
            reg_share = (wb["regression"] / total) * 100 if total else 0
            author_now.append((author, seconds_to_hours(total), round(reg_share, 1)))
    author_now.sort(key=lambda x: x[1], reverse=True)
    overloaded = author_now[:3]
    high_reg_share = [a for a in author_now if a[2] >= 40][:3]
    anomalies = []
    for author in unified_data.keys():
        cur = sum_author_week(unified_data[author], current_week)
        prev = sum_author_week(unified_data[author], previous_week)
        if prev <= 0:
            continue
        growth = ((cur - prev) / prev) * 100
        if growth >= ANOMALY_GROWTH_THRESHOLD_PCT:
            anomalies.append((author, seconds_to_hours(cur), seconds_to_hours(prev), round(growth, 1)))
    anomalies.sort(key=lambda x: x[3], reverse=True)
    anomalies = anomalies[:5]

    metric_table_style = "padding:7px; border:1px solid #dfe1e6; text-align:center;"
    report_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif; color:#172b4d;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <div>
          <h2 style="margin:0; font-size:24px;">QA Analytics Dashboard</h2>
          <div style="color:#6b778c; margin-top:4px;">Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')} · данные хранятся и считаются с {FIXED_START_DATE}</div>
        </div>
        <div style="padding:8px 12px; border-radius:16px; background:#deebff; color:#0747a6; font-weight:600;">Инкремент из Confluence</div>
      </div>
      <div style="display:grid; grid-template-columns:repeat(4,minmax(160px,1fr)); gap:12px; margin:16px 0 18px;">
        <div style="padding:14px; border-radius:12px; background:#e3fcef;"><div style="font-size:24px; font-weight:700; color:#006644;">{seconds_to_hours(current_total)} ч</div><div style="font-size:12px; color:#42526e;">Текущая неделя</div></div>
        <div style="padding:14px; border-radius:12px; background:#deebff;"><div style="font-size:24px; font-weight:700; color:#0747a6;">{seconds_to_hours(previous_total)} ч</div><div style="font-size:12px; color:#42526e;">Прошлая неделя · Δ {format_delta(current_total, previous_total)} ч</div></div>
        <div style="padding:14px; border-radius:12px; background:#ffebe6;"><div style="font-size:24px; font-weight:700; color:#bf2600;">{seconds_to_hours(current_regression)} ч</div><div style="font-size:12px; color:#42526e;">Регресс сейчас</div></div>
        <div style="padding:14px; border-radius:12px; background:#f3e8ff;"><div style="font-size:24px; font-weight:700; color:#5b2c83;">{seconds_to_hours(current_activities)} ч</div><div style="font-size:12px; color:#42526e;">Активности {SPECIAL_ACTIVITY_ISSUE}</div></div>
      </div>
      <p style="margin:0 0 12px; color:#42526e;">Фичи сейчас: <strong>{seconds_to_hours(current_features)} ч</strong> · Среднее за последние {len(rolling_weeks)} недели: <strong>{seconds_to_hours(rolling_avg)} ч/нед</strong>. Ниже — команды, средние по сотрудникам и недельная история.</p>
    """
    if chart_b64:
        report_html += f'<div style="margin-bottom:16px;"><img src="data:image/png;base64,{chart_b64}" style="max-width:100%; border:1px solid #dfe1e6; border-radius:10px;"/></div>'

    report_html += "<h3 style='margin:14px 0 8px;'>Инсайты текущей недели</h3>"
    report_html += "<div style='display:grid; grid-template-columns:repeat(3,minmax(220px,1fr)); gap:10px; margin-bottom:12px;'>"
    if overloaded:
        report_html += "<div style='padding:10px; border:1px solid #dfe1e6; border-radius:8px;'><div style='font-weight:700; margin-bottom:6px;'>Топ по нагрузке</div>"
        for name, hours, _ in overloaded:
            report_html += f"<div style='color:#172b4d;'>{html.escape(name)} — <strong>{hours} ч</strong></div>"
        report_html += "</div>"
    if high_reg_share:
        report_html += "<div style='padding:10px; border:1px solid #dfe1e6; border-radius:8px;'><div style='font-weight:700; margin-bottom:6px;'>Высокая доля регресса</div>"
        for name, hours, share in high_reg_share:
            report_html += f"<div style='color:#172b4d;'>{html.escape(name)} — {hours} ч, <strong>{share}% регресса</strong></div>"
        report_html += "</div>"
    if anomalies:
        report_html += f"<div style='padding:10px; border:1px solid #dfe1e6; border-radius:8px;'><div style='font-weight:700; margin-bottom:6px;'>Аномалии (рост &gt; {ANOMALY_GROWTH_THRESHOLD_PCT}%)</div>"
        for name, cur_h, prev_h, growth in anomalies:
            report_html += f"<div style='color:#172b4d;'>{html.escape(name)} — {prev_h} → <strong>{cur_h} ч</strong> ({growth}%)</div>"
        report_html += "</div>"
    report_html += "</div>"

    report_html += "<h3 style='margin:14px 0 8px;'>Команды: текущая неделя, дельта и состав</h3>"
    for team, authors in sorted(team_map.items()):
        team_current = sum(sum_author_week(unified_data[a], current_week) for a in authors)
        team_previous = sum(sum_author_week(unified_data[a], previous_week) for a in authors)
        team_ft = sum(week_bucket(unified_data[a], current_week)["features"] for a in authors)
        team_reg = sum(week_bucket(unified_data[a], current_week)["regression"] for a in authors)
        team_act = sum(week_bucket(unified_data[a], current_week)["activities"] for a in authors)
        if team_current == 0 and team_previous == 0:
            continue
        alert = " ⚠" if seconds_to_hours(team_current) >= TEAM_ALERT_HOURS else ""
        title = f"{team}: {seconds_to_hours(team_current)} ч сейчас · Δ {format_delta(team_current, team_previous)} ч ({format_delta_pct(team_current, team_previous)}){alert}"
        report_html += f"""
        <ac:structured-macro ac:name="expand">
          <ac:parameter ac:name="title">{html.escape(title)}</ac:parameter>
          <ac:rich-text-body>
            <table style="border-collapse:collapse; width:100%; margin-bottom:10px;">
              <thead><tr>
                <th style="{metric_table_style} background:#f4f5f7; text-align:left;">Сотрудник</th>
                <th style="{metric_table_style} background:#f4f5f7;">Текущая</th>
                <th style="{metric_table_style} background:#f4f5f7;">Прошлая</th>
                <th style="{metric_table_style} background:#f4f5f7;">Δ</th>
                <th style="{metric_table_style} background:#f4f5f7;">Фичи</th>
                <th style="{metric_table_style} background:#f4f5f7;">Регресс</th>
                <th style="{metric_table_style} background:#f4f5f7;">Активности</th>
              </tr></thead><tbody>
              <tr>
                <td style="{metric_table_style} text-align:left; font-weight:600;">Итого команда</td>
                <td style="{metric_table_style} font-weight:600;">{seconds_to_hours(team_current)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(team_previous)}</td>
                <td style="{metric_table_style}">{format_delta(team_current, team_previous)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(team_ft)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(team_reg)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(team_act)}</td>
              </tr>
        """
        for author in sorted(authors):
            cur = sum_author_week(unified_data[author], current_week)
            prev = sum_author_week(unified_data[author], previous_week)
            wb = week_bucket(unified_data[author], current_week)
            if cur == 0 and prev == 0:
                continue
            report_html += f"""
              <tr>
                <td style="{metric_table_style} text-align:left;">{html.escape(author)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(cur)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(prev)}</td>
                <td style="{metric_table_style}">{format_delta(cur, prev)}</td>
                <td style="{metric_table_style}">{seconds_to_hours(wb["features"])}</td>
                <td style="{metric_table_style}">{seconds_to_hours(wb["regression"])}</td>
                <td style="{metric_table_style}">{seconds_to_hours(wb["activities"])}</td>
              </tr>
            """
        report_html += "</tbody></table></ac:rich-text-body></ac:structured-macro>"

    report_html += generate_average_by_author_table(unified_data, author_project_stats)
    report_html += generate_weekly_report(unified_data, author_project_stats)

    report_html += '<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">📂 Детализация задач (весь период)</ac:parameter><ac:rich-text-body>'
    recent_projects = []
    for pk, pdata in by_project_data.items():
        stories = [s for s in pdata['stories'] if story_touches_weeks(s, report_weeks)]
        if stories:
            recent_projects.append((pk, stories))
    for pk, stories in sorted(recent_projects, key=lambda x: sum(s['total_time'] for s in x[1]), reverse=True):
        ph = seconds_to_hours(sum(s['total_time'] for s in stories))
        stories = sorted(stories, key=lambda x: x['total_time'], reverse=True)
        pk_esc = html.escape(pk)
        report_html += f"<h4>{pk_esc} ({ph}ч)</h4><table><thead><tr><th>Key</th><th>Type</th><th>Summary</th><th>Time</th><th>Authors</th></tr></thead><tbody>"
        for s in stories[:200]:
            key_esc = html.escape(s['key'])
            browse = f"{JIRA_URL}/browse/{key_esc}"
            report_html += f"""<tr><td><a href="{browse}">{key_esc}</a></td><td>{html.escape(s['issuetype'])}</td><td>{html.escape(s['summary'][:90])}</td><td><strong>{seconds_to_hours(s['total_time'])}</strong></td><td>{html.escape(", ".join(s['authors']))}</td></tr>"""
        report_html += "</tbody></table>"
    report_html += "</ac:rich-text-body></ac:structured-macro>"
    report_html += "</div>"
    return report_html

def create_charts(data):
    if not MATPLOTLIB_AVAILABLE: return None
    try:
        plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'bmh')
        fig = plt.figure(figsize=(18, 12))
        gs = fig.add_gridspec(2, 2)
        ax1 = fig.add_subplot(gs[0, :])
        authors = sorted(
            data['weekly_data'].keys(),
            key=lambda a: sum(d['features'] + d['regression'] + d['activities'] for d in data['weekly_data'][a].values()),
            reverse=True,
        )[:15]
        short_names = [n.split()[0] for n in authors]
        ft_times = [seconds_to_hours(sum(d['features'] for d in data['weekly_data'][a].values())) for a in authors]
        reg_times = [seconds_to_hours(sum(d['regression'] for d in data['weekly_data'][a].values())) for a in authors]
        act_times = [seconds_to_hours(sum(d['activities'] for d in data['weekly_data'][a].values())) for a in authors]
        ax1.bar(short_names, ft_times, label='Фичи', color='#3498db', alpha=0.8)
        ax1.bar(short_names, reg_times, bottom=ft_times, label='Регресс', color='#e74c3c', alpha=0.8)
        ax1.bar(short_names, act_times, bottom=np.array(ft_times) + np.array(reg_times), label='Активности', color='#8e44ad', alpha=0.8)
        ax1.set_title('Топ-15: Фичи / Регресс / Активности', fontweight='bold'); ax1.legend()
        ax2 = fig.add_subplot(gs[1, 0])
        all_weeks = set()
        for d in data['weekly_data'].values(): all_weeks.update(d.keys())
        all_weeks.add(calendar_week_monday_key())
        sorted_weeks = sorted(list(all_weeks))
        ft_w = [seconds_to_hours(sum(week_bucket(data['weekly_data'][a], w)['features'] for a in data['weekly_data'])) for w in sorted_weeks]
        reg_w = [seconds_to_hours(sum(week_bucket(data['weekly_data'][a], w)['regression'] for a in data['weekly_data'])) for w in sorted_weeks]
        act_w = [seconds_to_hours(sum(week_bucket(data['weekly_data'][a], w)['activities'] for a in data['weekly_data'])) for w in sorted_weeks]
        x = np.arange(len(sorted_weeks))
        disp_weeks = [datetime.strptime(w, '%Y-%m-%d').strftime('%d.%m') for w in sorted_weeks]
        ax2.bar(x, ft_w, label='Фичи', color='#3498db')
        ax2.bar(x, reg_w, bottom=ft_w, label='Регресс', color='#e74c3c')
        ax2.bar(x, act_w, bottom=np.array(ft_w) + np.array(reg_w), label='Активности', color='#8e44ad')
        ax2.set_xticks(x); ax2.set_xticklabels(disp_weeks); ax2.legend(); ax2.set_title('Динамика команды', fontweight='bold')
        ax3 = fig.add_subplot(gs[1, 1])
        project_totals = []
        for p, pdata in data['by_project'].items():
            total = sum(s['total_time'] for s in pdata['stories'] if story_touches_weeks(s, sorted_weeks))
            if total > 0:
                project_totals.append((p, seconds_to_hours(total)))
        project_totals.sort(key=lambda x: x[1])
        projs = [p for p, _ in project_totals]
        times = [t for _, t in project_totals]
        if projs:
            bars = ax3.barh(projs, times, color=plt.cm.Paired(np.linspace(0, 1, len(projs))))
            ax3.bar_label(bars, fmt='%.0f', padding=3)
        else:
            ax3.text(0.5, 0.5, 'Нет данных за период', ha='center', va='center')
        ax3.set_title('Фичи по проектам (весь период)', fontweight='bold')
        buffer = BytesIO()
        plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
        buffer.seek(0)
        b64 = base64.b64encode(buffer.read()).decode()
        plt.close()
        return b64
    except Exception:
        logger.exception("Не удалось построить графики")
        return None

def update_confluence_manual(page_id, new_html):
    """Полностью заменяет storage-тело страницы (не дописывает к старому контенту)."""
    page_id_str = str(page_id)
    full_html = new_html
    path = "rest/api/content/{0}".format(page_id_str)

    try:
        page = fetch_confluence_page(page_id_str, expand="version,space,title,ancestors")
        title = page["title"]
        space_key = page["space"]["key"]
        ver = page["version"]["number"]

        payload = {
            "id": page_id_str,
            "type": "page",
            "title": title,
            "status": "current",
            "space": {"key": space_key},
            "body": {"storage": {"value": full_html, "representation": "storage"}},
            "version": {"number": ver + 1, "message": "QA analytics refresh"},
        }

        upd = getattr(confluence_client, "update_page", None)
        if callable(upd):
            params = inspect.signature(upd).parameters
            kw = {}
            if "page_id" in params:
                kw["page_id"] = page_id_str
            if "title" in params:
                kw["title"] = title
            if "body" in params:
                kw["body"] = full_html
            if "type" in params:
                kw["type"] = "page"
            if "representation" in params:
                kw["representation"] = "storage"
            if "minor_edit" in params:
                kw["minor_edit"] = False
            if "version_comment" in params:
                kw["version_comment"] = "QA analytics refresh"
            if "parent_id" in params:
                anc = page.get("ancestors") or []
                if anc:
                    kw["parent_id"] = anc[-1]["id"]
            try:
                upd(**kw)
                logger.info("Confluence: страница %s заменена целиком (update_page)", page_id_str)
                return
            except Exception as e:
                logger.warning("update_page не применился (%s), пробуем PUT", e)

        put_fn = getattr(confluence_client, "put", None)
        if callable(put_fn):
            sig = inspect.signature(put_fn)
            if "json" in sig.parameters:
                put_fn(path, json=payload)
            else:
                put_fn(path, data=payload)
            logger.info("Confluence: страница %s заменена целиком (PUT)", page_id_str)
            return

        req_fn = getattr(confluence_client, "request", None)
        if callable(req_fn):
            rsig = inspect.signature(req_fn)
            if "json" in rsig.parameters and "path" in rsig.parameters and "method" in rsig.parameters:
                req_fn(method="PUT", path=path, json=payload)
            else:
                req_fn("PUT", path, json=payload)
            logger.info("Confluence: страница %s заменена целиком (request)", page_id_str)
            return

        raise RuntimeError("Не удалось вызвать update_page / put / request у клиента Confluence")
    except Exception as e:
        logger.error("Ошибка Confluence: %s", e)
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(new_html)

def main():
    logger.info("%s\nАнализ времени (Final Fixed v2)\n%s", "=" * 60, "=" * 60)
    unified_data, by_project_data, author_project_stats = create_data_store()
    fixed_start = datetime.strptime(FIXED_START_DATE, "%Y-%m-%d")
    page_state = load_page_state(PAGE_ID)
    run_seq = int((page_state or {}).get("run_seq", 0)) + 1
    force_full = (run_seq % FULL_RECONCILE_EVERY_N_RUNS == 0)

    if page_state and not force_full:
        recalc_start_key = normalize_recalc_week_start(INCREMENTAL_WEEKS)
        recalc_start = max(fixed_start, datetime.strptime(recalc_start_key, "%Y-%m-%d"))
        recalc_start_key = recalc_start.strftime("%Y-%m-%d")
        inject_cache_history(page_state, unified_data, by_project_data, author_project_stats, recalc_start_key)
        query_start = (recalc_start - timedelta(days=7)).strftime("%Y-%m-%d")
        logger.info("Инкрементальный режим из Confluence: пересчёт с %s (JQL updated >= %s)", recalc_start_key, query_start)
    else:
        recalc_start = fixed_start
        recalc_start_key = FIXED_START_DATE
        query_start = FIXED_START_DATE
        if force_full:
            logger.info("Периодический full reconcile (каждый %s запуск)", FULL_RECONCILE_EVERY_N_RUNS)
        else:
            logger.info("Полный пересчёт: служебное состояние на странице не найдено, старт с %s", FIXED_START_DATE)

    analyze_features(recalc_start, query_start, unified_data, by_project_data, author_project_stats)
    analyze_regression(recalc_start, query_start, unified_data, author_project_stats)
    analyze_special_activity(SPECIAL_ACTIVITY_ISSUE, recalc_start, unified_data, by_project_data, author_project_stats)

    if page_state and not force_full and not validate_incremental_integrity(page_state, unified_data, recalc_start_key):
        logger.warning("Переход на full reconcile из-за нарушения инвариантов")
        unified_data, by_project_data, author_project_stats = create_data_store()
        analyze_features(fixed_start, FIXED_START_DATE, unified_data, by_project_data, author_project_stats)
        analyze_regression(fixed_start, FIXED_START_DATE, unified_data, author_project_stats)
        analyze_special_activity(SPECIAL_ACTIVITY_ISSUE, fixed_start, unified_data, by_project_data, author_project_stats)

    chart = create_charts({'weekly_data': unified_data, 'by_project': by_project_data})
    html_content = generate_html_report(unified_data, by_project_data, author_project_stats, chart)
    state_payload = build_page_state(unified_data, by_project_data, author_project_stats)
    state_payload["run_seq"] = run_seq
    update_confluence_manual(PAGE_ID, html_content)
    save_state_to_confluence(PAGE_ID, STATE_PROPERTY_KEY, state_payload, html_content)

if __name__ == "__main__":
    main()
