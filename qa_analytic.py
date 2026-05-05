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
REPORT_WEEKS = 8
SPECIAL_ACTIVITY_ISSUE = "HRC-3630"
STATE_START = "QA_ANALYTICS_STATE_V1_START"
STATE_END = "QA_ANALYTICS_STATE_V1_END"

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

def rate_limited_request(func, *args, **kwargs):
    """Один активный запрос к API за раз; пауза REQUEST_DELAY между успешными вызовами."""
    global last_request_time
    last_err = None
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
            if JIRAError is not None and isinstance(e, JIRAError):
                sc = getattr(e, "status_code", None)
                if sc in (401, 403):
                    logger.error("Jira: доступ запрещён или неверная авторизация: %s", e)
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
        dates = str(story.get("worklog_dates", "")).split(",")
        dates = [d.strip() for d in dates if d.strip() and d.strip() != "Нет данных"]
        if not dates:
            return False
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

def get_week_start(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    start = dt - timedelta(days=dt.weekday())
    return start.strftime('%Y-%m-%d')

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

def fetch_linked_tasks_bulk(parent_issues):
    linked_keys = set()
    for issue in parent_issues:
        if hasattr(issue.fields, 'issuelinks'):
            for link in issue.fields.issuelinks:
                target = getattr(link, 'outwardIssue', getattr(link, 'inwardIssue', None))
                if target: linked_keys.add(target.key)
    if not linked_keys: return {}
    linked_tasks_map = {}
    linked_keys_list = list(linked_keys)
    for i in range(0, len(linked_keys_list), 50):
        batch_keys = linked_keys_list[i:i+50]
        keys_str = ",".join(batch_keys)
        jql = f"key in ({keys_str}) AND issuetype in (Task, Sub-task, Подзадача, Задача)"
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
        batch = rate_limited_request(jira_client.search_issues, jql, startAt=start_at, maxResults=100, fields='summary,priority,customfield_18300,issuelinks,worklog,project,issuetype')
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
        story_wls = issue.fields.worklog.worklogs if hasattr(issue.fields, 'worklog') else []
        valid_story_wls, _ = parse_worklogs_local(story_wls, start_date_obj)
        story_time = sum(w['timeSpentSeconds'] for w in valid_story_wls)
        linked_time, linked_tasks_info, weekly_stats, all_dates, all_authors, project_stats = 0, [], [], set(), set(), defaultdict(int)
        for wl in valid_story_wls:
            weekly_stats.append({'author': wl['author_name'], 'week': wl['week_start'], 'time': wl['timeSpentSeconds']})
            project_stats[wl['author_name']] += wl['timeSpentSeconds']
            all_dates.add(wl['started'][:10])
            all_authors.add(wl['author_name'])
        if hasattr(issue.fields, 'issuelinks'):
            for link in issue.fields.issuelinks:
                target = getattr(link, 'outwardIssue', getattr(link, 'inwardIssue', None))
                if target and target.key in linked_map:
                    task = linked_map[target.key]
                    if 'тестирование' not in task.fields.summary.lower(): continue
                    t_wls = task.fields.worklog.worklogs if hasattr(task.fields, 'worklog') else []
                    valid_t_wls, _ = parse_worklogs_local(t_wls, start_date_obj)
                    t_time = sum(w['timeSpentSeconds'] for w in valid_t_wls)
                    if t_time > 0:
                        linked_time += t_time
                        for w in valid_t_wls:
                            weekly_stats.append({'author': w['author_name'], 'week': w['week_start'], 'time': w['timeSpentSeconds']})
                            project_stats[w['author_name']] += w['timeSpentSeconds']
                            all_dates.add(w['started'][:10])
                            all_authors.add(w['author_name'])
                        linked_tasks_info.append({'key': task.key, 'time': t_time})
        if linked_time > 0: total_time, source = linked_time, "Task"
        else: total_time, source = story_time, "Story"
        if total_time == 0: return None
        thread_safe_log_info(f"✓ {issue.key} ({project_key}) - {seconds_to_hours(total_time)}ч")
        return {'project_key': project_key, 'total_time': total_time, 'weekly_stats': weekly_stats, 'project_stats': dict(project_stats),
                'story_data': {'key': issue.key, 'issuetype': issue.fields.issuetype.name, 'summary': issue.fields.summary, 'source': source,
                               'total_time': total_time, 'worklog_dates': ', '.join(sorted(list(all_dates))) if all_dates else 'Нет данных',
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
            wls = t.fields.worklog.worklogs if hasattr(t.fields, 'worklog') else []
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

    worklogs = issue.fields.worklog.worklogs if hasattr(issue.fields, "worklog") else []
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
    by_project_data[project_key]["stories"].append(
        {
            "key": issue.key,
            "issuetype": issue.fields.issuetype.name if hasattr(issue.fields, "issuetype") else "Task",
            "summary": f"{issue.fields.summary} (активности)",
            "source": "Activity",
            "total_time": total,
            "worklog_dates": ", ".join(sorted(by_week.keys())),
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


def ordered_report_weeks(unified_data, limit=REPORT_WEEKS, ref_date=None):
    current_week_key = calendar_week_monday_key(ref_date)
    all_weeks = set()
    for auth_data in unified_data.values():
        all_weeks.update(auth_data.keys())
    all_weeks.add(current_week_key)
    return sorted(all_weeks, reverse=True)[:limit]


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


def story_touches_weeks(story, weeks):
    target_weeks = set(weeks)
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


def generate_weekly_report(unified_data, author_project_stats, ref_date=None):
    if not unified_data: return ""

    ref_date = ref_date or datetime.now()
    current_week_key = calendar_week_monday_key(ref_date)

    sorted_weeks = ordered_report_weeks(unified_data, REPORT_WEEKS, ref_date)

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

    full_html = f"<h3>📊 Нагрузка по неделям (последние {REPORT_WEEKS})</h3>"

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
    report_weeks = ordered_report_weeks(unified_data, REPORT_WEEKS)
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

    metric_table_style = "padding:7px; border:1px solid #dfe1e6; text-align:center;"
    report_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif; color:#172b4d;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <div>
          <h2 style="margin:0; font-size:24px;">QA Analytics Dashboard</h2>
          <div style="color:#6b778c; margin-top:4px;">Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')} · срез: текущая неделя + rolling {REPORT_WEEKS} недель</div>
        </div>
        <div style="padding:8px 12px; border-radius:16px; background:#deebff; color:#0747a6; font-weight:600;">Инкремент из Confluence</div>
      </div>
      <div style="display:grid; grid-template-columns:repeat(4,minmax(160px,1fr)); gap:12px; margin:16px 0 18px;">
        <div style="padding:14px; border-radius:12px; background:#e3fcef;"><div style="font-size:24px; font-weight:700; color:#006644;">{seconds_to_hours(current_total)} ч</div><div style="font-size:12px; color:#42526e;">Текущая неделя</div></div>
        <div style="padding:14px; border-radius:12px; background:#deebff;"><div style="font-size:24px; font-weight:700; color:#0747a6;">{seconds_to_hours(previous_total)} ч</div><div style="font-size:12px; color:#42526e;">Прошлая неделя · Δ {format_delta(current_total, previous_total)} ч</div></div>
        <div style="padding:14px; border-radius:12px; background:#ffebe6;"><div style="font-size:24px; font-weight:700; color:#bf2600;">{seconds_to_hours(current_regression)} ч</div><div style="font-size:12px; color:#42526e;">Регресс сейчас</div></div>
        <div style="padding:14px; border-radius:12px; background:#f3e8ff;"><div style="font-size:24px; font-weight:700; color:#5b2c83;">{seconds_to_hours(current_activities)} ч</div><div style="font-size:12px; color:#42526e;">Активности {SPECIAL_ACTIVITY_ISSUE}</div></div>
      </div>
      <p style="margin:0 0 12px; color:#42526e;">Фичи сейчас: <strong>{seconds_to_hours(current_features)} ч</strong> · Среднее за последние {len(rolling_weeks)} недели: <strong>{seconds_to_hours(rolling_avg)} ч/нед</strong>. Ниже — раскрываемые команды и сотрудники.</p>
    """
    if chart_b64:
        report_html += f'<div style="margin-bottom:16px;"><img src="data:image/png;base64,{chart_b64}" style="max-width:100%; border:1px solid #dfe1e6; border-radius:10px;"/></div>'

    report_html += "<h3 style='margin:14px 0 8px;'>Команды: текущая неделя, дельта и состав</h3>"
    for team, authors in sorted(team_map.items()):
        team_current = sum(sum_author_week(unified_data[a], current_week) for a in authors)
        team_previous = sum(sum_author_week(unified_data[a], previous_week) for a in authors)
        team_ft = sum(week_bucket(unified_data[a], current_week)["features"] for a in authors)
        team_reg = sum(week_bucket(unified_data[a], current_week)["regression"] for a in authors)
        team_act = sum(week_bucket(unified_data[a], current_week)["activities"] for a in authors)
        if team_current == 0 and team_previous == 0:
            continue
        title = f"{team}: {seconds_to_hours(team_current)} ч сейчас · Δ {format_delta(team_current, team_previous)} ч"
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

    report_html += generate_weekly_report(unified_data, author_project_stats)

    report_html += f'<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">📂 Детализация задач за последние {REPORT_WEEKS} недель</ac:parameter><ac:rich-text-body>'
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
        sorted_weeks = sorted(list(all_weeks))[-REPORT_WEEKS:]
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
        ax3.set_title(f'Фичи по проектам ({REPORT_WEEKS} нед.)', fontweight='bold')
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

    if page_state:
        recalc_start = max(
            fixed_start,
            datetime.now() - timedelta(weeks=INCREMENTAL_WEEKS),
        )
        recalc_start_key = recalc_start.strftime("%Y-%m-%d")
        inject_cache_history(page_state, unified_data, by_project_data, author_project_stats, recalc_start_key)
        query_start = (recalc_start - timedelta(days=7)).strftime("%Y-%m-%d")
        logger.info("Инкрементальный режим из Confluence: пересчёт с %s (JQL updated >= %s)", recalc_start_key, query_start)
    else:
        recalc_start = fixed_start
        recalc_start_key = FIXED_START_DATE
        query_start = FIXED_START_DATE
        logger.info("Полный пересчёт: служебное состояние на странице не найдено, старт с %s", FIXED_START_DATE)

    analyze_features(recalc_start, query_start, unified_data, by_project_data, author_project_stats)
    analyze_regression(recalc_start, query_start, unified_data, author_project_stats)
    analyze_special_activity(SPECIAL_ACTIVITY_ISSUE, recalc_start, unified_data, by_project_data, author_project_stats)
    chart = create_charts({'weekly_data': unified_data, 'by_project': by_project_data})
    html_content = generate_html_report(unified_data, by_project_data, author_project_stats, chart)
    html_content += render_page_state(build_page_state(unified_data, by_project_data, author_project_stats))
    update_confluence_manual(PAGE_ID, html_content)

if __name__ == "__main__":
    main()
