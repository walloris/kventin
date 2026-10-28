"""
Модуль для работы с GigaChat API
Оптимизирован для структурированных ответов с автозаполнением
"""

import json
import logging
import re
import sys
import time
import warnings
from pathlib import Path
from typing import List, Dict, Optional

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

script_dir = Path(__file__).resolve().parent
parent_dir = script_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from config import config

# Подавление SSL warnings
urllib3.disable_warnings(InsecureRequestWarning)
warnings.filterwarnings('ignore', category=InsecureRequestWarning)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ========================================
# ПОЛУЧЕНИЕ ТОКЕНОВ
# ========================================

def get_gigachat_token(env: str) -> Optional[str]:
    """Получает OAuth токен через POST запрос к Keycloak (имитация Insomnia)"""
    try:
        url = config['gigachat'][f'token_url_{env}']
        person_id = config['gigachat'][f'person_id_{env}']

        # 1. Заголовки как в Insomnia + маскировка User-Agent
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'x-hrp-person-id': person_id,
            'User-Agent': 'insomnia/8.6.1',  # Притворяемся Инсомнией
            'Accept': '*/*'
        }

        # 2. Куки отдельно (надежнее, чем в headers)
        cookies = {
            'KEYCLOAK_LOCALE': 'ru'
        }

        # 3. Тело запроса
        # ВАЖНО: Убедись, что в конфиге client_id совпадает с тем, что в cURL ('fakeuser')
        payload = {
            'grant_type': 'password',
            'username': config['gigachat'].get('username', ''),
            'password': config['gigachat'].get('password', ''),
            'client_id': config['gigachat'].get('client_id', 'fakeuser')
        }

        logger.info(f"🔗 Получение токена из: {url}")
        logger.info(f"🆔 Person ID: {person_id}")
        logger.debug(f"📤 Payload client_id: {payload.get('client_id')}")

        # 4. Сам запрос
        response = requests.post(
            url,
            data=payload,
            headers=headers,
            cookies=cookies,     # Передаем куки сюда
            verify=False,        # Отключаем проверку SSL (как в cURL --insecure)
            timeout=60
        )

        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')

            if access_token:
                logger.info("✅ Токен успешно получен")
                return access_token
            else:
                logger.error("❌ Ответ 200, но нет access_token внутри JSON")
                return None
        else:
            logger.error(f"❌ Ошибка авторизации HTTP {response.status_code}")
            logger.error(f"❌ Ответ сервера: {response.text}")

            # Если 401 - скорее всего пароль или client_id не тот
            if response.status_code == 401:
                logger.warning("⚠️ Проверь пароль и client_id в config.py. В cURL client_id=fakeuser")

            return None

    except Exception as e:
        logger.error(f"❌ Критическая ошибка requests: {e}", exc_info=True)
        return None



# ========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПАРСИНГА
# ========================================

def parse_gigachat_response(response_text: str) -> List[Dict]:
    """Парсинг ответа GigaChat с поддержкой разных форматов"""
    if not response_text:
        logger.warning("⚠️ Пустой ответ")
        return []

    # Попытка 1: Прямой JSON
    try:
        data = json.loads(response_text)
        if isinstance(data, dict) and "analysis" in data:
            logger.debug("✅ Прямой JSON с analysis")
            return data["analysis"]
        elif isinstance(data, list):
            logger.debug("✅ Прямой JSON как список")
            return data
    except json.JSONDecodeError:
        pass

    # Попытка 2: Извлечение через regex
    try:
        json_match = re.search(
            r'\{[\s\S]*?"analysis"[\s\S]*?\][\s\S]*?\}',
            response_text,
            re.DOTALL
        )
        if json_match:
            data = json.loads(json_match.group(0))
            if "analysis" in data:
                logger.debug("✅ Извлечение JSON")
                return data["analysis"]
    except Exception:
        pass

    # Попытка 3: Очистка markdown
    try:
        cleaned = response_text.strip()
        # Удаляем markdown код-блоки - используем переменные
        triple_backticks = '```'
        cleaned = re.sub(f'^{re.escape(triple_backticks)}(?:json)?\\s*', '', cleaned)
        cleaned = re.sub(f'\\s*{re.escape(triple_backticks)}$', '', cleaned)
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        if isinstance(data, dict) and "analysis" in data:
            logger.debug("✅ Очистка markdown")
            return data["analysis"]
        elif isinstance(data, list):
            return data
    except Exception:
        pass

    logger.error("❌ Не удалось распарсить ответ")
    logger.debug(f"Ответ: {response_text[:1000]}...")
    return []


def fix_incomplete_recommendations(analysis: List[Dict]) -> List[Dict]:
    """
    Автоматически заполняет отсутствующие поля recommendations.
    """
    fixed_analysis = []

    for item in analysis:
        # Проверяем наличие recommendations
        if 'recommendations' not in item or not isinstance(item['recommendations'], dict):
            item['recommendations'] = {}

        recs = item['recommendations']

        # Заполняем missing_cause
        if 'missing_cause' not in recs or not recs.get('missing_cause'):
            if item.get('is_cause_correct') is False:
                recs['missing_cause'] = '[WARN] Причина пропуска не указана или некорректна'
            else:
                recs['missing_cause'] = '[OK] Причина пропуска корректна'

        # Заполняем description_analysis
        if 'description_analysis' not in recs or not recs.get('description_analysis'):
            if item.get('is_description_good') is False:
                recs['description_analysis'] = '[ERROR] Описание неполное, отсутствуют детали'
            else:
                recs['description_analysis'] = '[OK] Описание содержит достаточно информации'

        # Заполняем priority_analysis
        if 'priority_analysis' not in recs or not recs.get('priority_analysis'):
            if item.get('is_priority_correct') is False:
                recs['priority_analysis'] = '[ERROR] Приоритет указан некорректно'
            else:
                recs['priority_analysis'] = '[OK] Приоритет корректен'

        item['recommendations'] = recs
        fixed_analysis.append(item)

    return fixed_analysis


def validate_gigachat_analysis(analysis: List[Dict], original_batch: List[Dict]) -> bool:
    """Проверяет качество анализа с учетом автозаполнения"""
    if not analysis:
        logger.warning("⚠️ Пустой анализ")
        return False

    if len(analysis) != len(original_batch):
        logger.warning(f"⚠️ Несоответствие: ожидалось {len(original_batch)}, получено {len(analysis)}")

    required_fields = ['key', 'needs_comment', 'is_cause_correct',
                       'is_priority_correct', 'is_description_good', 'recommendations']

    valid_count = 0
    for item in analysis:
        if not all(field in item for field in required_fields):
            missing = [f for f in required_fields if f not in item]
            logger.warning(f"⚠️ {item.get('key', 'UNKNOWN')} - отсутствуют: {missing}")
            continue

        recs = item.get('recommendations', {})
        if not isinstance(recs, dict):
            logger.warning(f"⚠️ {item.get('key')} - recommendations не dict")
            continue

        required_rec_fields = ['missing_cause', 'description_analysis', 'priority_analysis']
        if all(field in recs and recs.get(field) for field in required_rec_fields):
            valid_count += 1
        else:
            missing_recs = [f for f in required_rec_fields if not recs.get(f)]
            logger.warning(f"⚠️ {item.get('key')} - пустые: {missing_recs}")

    validity_rate = valid_count / len(analysis) if analysis else 0
    logger.info(f"📊 Качество: {validity_rate * 100:.1f}% ({valid_count}/{len(analysis)})")

    is_valid = validity_rate >= 0.6

    if not is_valid:
        logger.warning(f"⚠️ Не прошел валидацию: {validity_rate * 100:.1f}% < 60%")

    return is_valid


# ========================================
# АНАЛИЗ ДЕФЕКТОВ
# ========================================


def bugs_analyse_by_gigachat(env: str, batch: List[Dict], access_token: str, max_retries: int = 3) -> Optional[Dict]:
    """
    ОПТИМИЗИРОВАННЫЙ анализ дефектов через GigaChat.
    РЕЖИМ: СТРОГИЙ АУДИТОР (Production версия).
    """
    try:
        api_url = config['gigachat'][f'api_url_{env}']
        logger.info(f"🔗 Отправка пакета из {len(batch)} дефектов")

        # 1. Формируем промпт (СТРОГИЙ)
        prompt = """Ты — строгий QA-Аудитор. Твоя задача — НАЙТИ ОШИБКИ в оформлении дефектов.
Ты должен быть скептичным. По умолчанию считай, что дефект оформлен ПЛОХО.

ФОРМАТ ОТВЕТА (JSON):
{{
  "analysis": [
    {{
      "key": "BUG-123",
      "needs_comment": true,
      "is_cause_correct": false,
      "is_priority_correct": false,
      "is_description_good": false,
      "recommendations": {{
        "missing_cause": "[WARN] Поле не заполнено. Предлагаю из списка: Тест-кейс не выполнен",
        "description_analysis": "[ERROR] Описание слишком краткое",
        "priority_analysis": "[ERROR] Указан Minor, но проблема блокирует функционал"
      }}
    }}
  ]
}}

⚠️ СПИСОК ПРИЧИН ПРОПУСКА (ТОЛЬКО ЭТИ):
1. Неполнота тестовой модели
2. Не выполнены требования кибербезопасности
3. Тест-кейс не запланирован
4. Тест-кейс не выполнен
5. Некорректные тестовые данные
6. Нарушение процедуры тестирования
7. Несоответствие тестового окружения
8. Ошибка при установке
9. Неакцептованные изменения в ППО/СПО
10. Пропущен этап тестирования
11. Ошибка в согласованных требованиях
12. Несогласованные изменения требований
13. Отсутствие требований
14. Изменение/Проблема на стороне партнеров/провайдеров
15. Ошибка была известна в релизе

🛑 КРИТЕРИИ БРАКОВКИ (FAIL CRITERIA):
1. ПРИЧИНА: Если пусто или нет в списке -> is_cause_correct = false.
2. ОПИСАНИЕ: Если короче 50 символов или нет шагов -> is_description_good = false.
3. ПРИОРИТЕТ: Если блокирует работу, а приоритет низкий -> is_priority_correct = false.

ДАННЫЕ ДЛЯ АУДИТА:
{batch_json}

JSON:"""

        # 2. Подготовка данных
        filled_batch = []
        for item in batch:
            if item is None:
                continue

            filled_item = {
                'key': item.get('key', 'UNKNOWN'),
                'cause': item.get('cause') if item.get('cause') else '[EMPTY]',
                'priority': item.get('priority', 'Unknown'),
            }

            desc = item.get('description', '')
            if not desc:
                filled_item['description'] = '[EMPTY]'
            elif len(desc) < 30:
                filled_item['description'] = f"{desc} (ОЧЕНЬ КОРОТКОЕ ОПИСАНИЕ)"
            else:
                filled_item['description'] = desc[:1000] + '...' if len(desc) > 1000 else desc

            filled_batch.append(filled_item)

        rendered_prompt = prompt.format(batch_json=json.dumps(filled_batch, ensure_ascii=False, indent=2))

        # 3. Формируем Payload
        payload = {
            "model": config['gigachat'].get('model', 'GigaChat'),
            "messages": [
                {
                    "role": "system",
                    "content": "Ты — строгий QA Lead. Твоя цель — найти ошибки в оформлении дефектов. Отклоняй любые недочеты."
                },
                {
                    "role": "user",
                    "content": rendered_prompt
                }
            ],
            "temperature": 0.1,
            "top_p": 0.95,
            "repetition_penalty": 1.15,
            "stream": False
        }

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}'
        }

        # 4. Цикл отправки
        for attempt in range(max_retries):
            try:
                logger.info(f"🔄 Попытка {attempt + 1}/{max_retries}")

                response = requests.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    verify=False,
                    timeout=150
                )

                if response.status_code == 200:
                    try:
                        result = response.json()
                    except json.JSONDecodeError:
                        logger.error("❌ Ошибка: Ответ не JSON")
                        continue

                    choices = result.get('choices', [])
                    if not choices:
                        logger.error("❌ Ответ пуст (нет choices)")
                        continue

                    content = choices[0].get('message', {}).get('content', '')

                    if not content:
                        logger.warning("⚠️ Внимание: Пустой контент ответа")
                        time.sleep(3)
                        continue

                    logger.info(f"📊 Ответ получен ({len(content)} симв)")

                    parsed = parse_gigachat_response(content)

                    if parsed:
                        parsed = fix_incomplete_recommendations(parsed)
                        if validate_gigachat_analysis(parsed, filled_batch):
                            logger.info(f"✅ Аудит завершен: {len(parsed)} дефектов")
                            return result
                        elif attempt < max_retries - 1:
                            logger.warning("⚠️ Ответ не прошел валидацию структуры, повторяем...")
                            time.sleep(3)
                            continue
                    else:
                        logger.error("❌ Не удалось распарсить JSON")
                        if attempt < max_retries - 1:
                            time.sleep(3)
                            continue

                elif response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 10))
                    logger.warning(f"⚠️ Rate limit: {retry_after}с")
                    time.sleep(retry_after)
                    continue

                elif response.status_code == 401:
                    logger.error("🔐 Токен истек")
                    return None

                else:
                    logger.error(f"❌ HTTP {response.status_code}: {response.text[:200]}")
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    return None

            except Exception as e:
                logger.error(f"❌ Ошибка запроса: {e}", exc_info=True)
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue

        return None

    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        return None


# ========================================
# АНАЛИЗ ТЕСТ-КЕЙСОВ
# ========================================

def review_test_case_by_gigachat(env: str, prompt: str, token: str, max_retries: int = 3) -> Optional[Dict]:
    """Анализ тест-кейса через GigaChat"""
    try:
        api_url = config['gigachat'][f'api_url_{env}']

        payload = {
            "model": config['gigachat']['model'],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": False
        }

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(api_url, json=payload, headers=headers, verify=False, timeout=60)

                if response.status_code == 200:
                    logger.info("✅ Ответ получен")
                    return response.json()
                elif response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 10))
                    time.sleep(retry_after)
                    continue
                elif response.status_code == 401:
                    logger.error("🔐 Токен истек")
                    return None
                else:
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None

            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

        return None

    except Exception as e:
        logger.error(f"❌ Ошибка анализа тест-кейса: {e}", exc_info=True)
        return None


# ========================================
# ПРОВЕРКА СООТВЕТСТВИЯ SUMMARY <-> DESCRIPTION
# ========================================

SUMMARY_DESCRIPTION_PROMPT = """Ты — строгий QA-Аудитор. Тебе дан список задач Jira (Story/Bug).
Для каждой задачи определи, соответствует ли её НАЗВАНИЕ (summary) семантическому содержанию ОПИСАНИЯ (description).

Правила:
- is_match = true, если summary в общих чертах описывает то, что в description (одна и та же предметная область, тот же объект, действие или дефект).
- is_match = false, если summary говорит об одном, а description о другом — другой компонент, другой функционал, другой баг.
- Краткость summary — это нормально, главное — отсутствие смыслового противоречия с description.
- Если description пустое или явно бессмысленное (мусор, "тест", "asdf") — is_match = false, reason укажи "Описание не информативно".
- reason — короткая строка (до 200 символов) на русском.

ФОРМАТ ОТВЕТА — строго JSON, без markdown, без комментариев:
{{
  "analysis": [
    {{"key": "ABC-123", "is_match": true, "reason": "Название и описание про одно и то же"}},
    {{"key": "ABC-124", "is_match": false, "reason": "Summary про логин, description про экспорт отчёта"}}
  ]
}}

ДАННЫЕ ДЛЯ АУДИТА:
{batch_json}

JSON:"""


def _parse_summary_match_response(response_text: str) -> List[Dict]:
    """Парсит ответ GigaChat для проверки summary↔description."""
    if not response_text:
        return []

    try:
        data = json.loads(response_text)
        if isinstance(data, dict) and "analysis" in data:
            return data["analysis"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    try:
        match = re.search(r'\{[\s\S]*?"analysis"[\s\S]*?\][\s\S]*?\}', response_text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if "analysis" in data:
                return data["analysis"]
    except Exception:
        pass

    try:
        cleaned = response_text.strip()
        triple = '```'
        cleaned = re.sub(f'^{re.escape(triple)}(?:json)?\\s*', '', cleaned)
        cleaned = re.sub(f'\\s*{re.escape(triple)}$', '', cleaned)
        cleaned = cleaned.strip()
        data = json.loads(cleaned)
        if isinstance(data, dict) and "analysis" in data:
            return data["analysis"]
        if isinstance(data, list):
            return data
    except Exception:
        pass

    logger.error("❌ Не удалось распарсить ответ GigaChat (summary-match)")
    logger.debug(f"Ответ: {response_text[:1000]}")
    return []


def check_summary_description_match(
    env: str,
    batch: List[Dict],
    access_token: str,
    max_retries: int = 3,
) -> Optional[List[Dict]]:
    """
    Проверяет соответствие summary↔description через GigaChat.

    :param env: 'ift' или 'dev'
    :param batch: список задач [{'key': 'ABC-1', 'summary': '...', 'description': '...'}]
    :param access_token: OAuth токен GigaChat
    :param max_retries: число повторов при сетевых ошибках/429
    :return: список [{'key': str, 'is_match': bool, 'reason': str}] или None при ошибке.
    """
    if not batch:
        return []

    try:
        api_url = config['gigachat'][f'api_url_{env}']
    except KeyError:
        logger.error(f"❌ В config нет gigachat.api_url_{env}")
        return None

    filled_batch: List[Dict] = []
    for item in batch:
        if item is None:
            continue
        filled_batch.append({
            'key': item.get('key', 'UNKNOWN'),
            'summary': (item.get('summary') or '').strip() or '[EMPTY]',
            'description': (item.get('description') or '').strip() or '[EMPTY]',
        })

    if not filled_batch:
        return []

    rendered_prompt = SUMMARY_DESCRIPTION_PROMPT.format(
        batch_json=json.dumps(filled_batch, ensure_ascii=False, indent=2)
    )

    payload = {
        "model": config['gigachat'].get('model', 'GigaChat'),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — строгий QA Lead. Сравниваешь название и описание задач Jira. "
                    "Возвращай только валидный JSON, без markdown."
                ),
            },
            {"role": "user", "content": rendered_prompt},
        ],
        "temperature": 0.0,
        "top_p": 0.95,
        "stream": False,
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {access_token}',
    }

    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 Summary-match: попытка {attempt + 1}/{max_retries} ({len(filled_batch)} задач)")
            response = requests.post(
                api_url,
                json=payload,
                headers=headers,
                verify=False,
                timeout=150,
            )

            if response.status_code == 200:
                try:
                    result = response.json()
                except json.JSONDecodeError:
                    logger.error("❌ Ответ GigaChat не JSON")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None

                choices = result.get('choices', [])
                if not choices:
                    logger.error("❌ Ответ GigaChat без choices")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None

                content = choices[0].get('message', {}).get('content', '')
                if not content:
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None

                parsed = _parse_summary_match_response(content)
                if not parsed:
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    return None

                normalized: List[Dict] = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get('key', '')).strip()
                    if not key:
                        continue
                    is_match_raw = item.get('is_match')
                    if isinstance(is_match_raw, str):
                        is_match = is_match_raw.strip().lower() in ('true', 'yes', 'да', '1')
                    else:
                        is_match = bool(is_match_raw)
                    reason = str(item.get('reason', '')).strip()
                    normalized.append({'key': key, 'is_match': is_match, 'reason': reason})
                return normalized

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                logger.warning(f"⚠️ Rate limit GigaChat: {retry_after}с")
                time.sleep(retry_after)
                continue

            if response.status_code == 401:
                logger.error("🔐 Токен GigaChat истёк")
                return None

            logger.error(f"❌ GigaChat HTTP {response.status_code}: {response.text[:200]}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            return None

        except Exception as e:
            logger.error(f"❌ Ошибка запроса GigaChat (summary-match): {e}", exc_info=True)
            if attempt < max_retries - 1:
                time.sleep(5)
                continue

    return None
