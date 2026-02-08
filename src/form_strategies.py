"""
Умные стратегии заполнения форм: happy path, negative, boundary, security.
По inputType/placeholder/name генерирует подходящие тестовые данные.
"""
import random
from typing import Dict, List, Optional

# Стратегии заполнения
STRATEGIES = ["happy", "negative", "boundary", "security"]

# Тестовые данные по типу поля
HAPPY_PATH = {
    "email": "test@example.com",
    "tel": "+79991234567",
    "phone": "+79991234567",
    "password": "TestPass123!",
    "number": "42",
    "url": "https://example.com",
    "search": "тестовый запрос",
    "date": "2025-01-15",
    "text": "Иван Тестов",
    "name": "Иван Тестов",
    "firstname": "Иван",
    "lastname": "Тестов",
    "city": "Москва",
    "address": "ул. Тестовая, д. 1",
    "zip": "123456",
    "comment": "Тестовый комментарий для проверки формы",
    "message": "Тестовое сообщение от AI-тестировщика",
    "default": "test value",
}

NEGATIVE = {
    "email": ["test", "@", "test@", "@test.com", "test@@test.com", "тест@тест.рф", ""],
    "tel": ["abc", "123", "+7", "++79991234567", "0", ""],
    "phone": ["abc", "123", "+7", "++79991234567", ""],
    "password": ["1", "abc", "", "   "],
    "number": ["abc", "-1", "0", "99999999", "3.14", "", "NaN"],
    "url": ["http", "://", "not-a-url", "ftp://test", ""],
    "date": ["2025-13-32", "abc", "0000-00-00", ""],
    "text": ["", "   ", "a", "<script>alert(1)</script>"],
    "default": ["", "   ", "a"],
}

BOUNDARY = {
    "email": ["a@b.c", "x" * 200 + "@test.com", "test+tag@example.com"],
    "tel": ["+7" + "9" * 20, "0", "+0"],
    "password": ["a", "a" * 256, "🔒" * 10],
    "number": ["0", "-1", "2147483647", "-2147483648", "0.001"],
    "text": ["a", "a" * 256, " " * 50 + "text", "абвгдеёжзийклмнопрстуфхцчшщъыьэюя" * 5],
    "default": ["a", "a" * 256],
}

SECURITY = {
    "default": [
        "<script>alert('xss')</script>",
        "'; DROP TABLE users; --",
        "\" OR 1=1 --",
        "{{7*7}}",
        "${7*7}",
        "<img src=x onerror=alert(1)>",
        "javascript:alert(1)",
        "../../../etc/passwd",
        "%00",
        "\\n\\r\\n",
    ],
}


def detect_field_type(input_type: str = "", placeholder: str = "", name: str = "", aria_label: str = "") -> str:
    """Определить тип поля по атрибутам для выбора стратегии заполнения."""
    combined = f"{input_type} {placeholder} {name} {aria_label}".lower()
    if input_type in ("email",) or "email" in combined or "e-mail" in combined or "почт" in combined:
        return "email"
    if input_type in ("tel",) or "phone" in combined or "телефон" in combined or "моб" in combined:
        return "tel"
    if input_type in ("password",) or "пароль" in combined or "password" in combined:
        return "password"
    if input_type in ("number",) or "число" in combined or "amount" in combined or "сумм" in combined:
        return "number"
    if input_type in ("url",) or "url" in combined or "сайт" in combined or "ссылк" in combined:
        return "url"
    if input_type in ("date", "datetime-local"):
        return "date"
    if input_type in ("search",) or "поиск" in combined or "search" in combined:
        return "search"
    if "name" in combined or "имя" in combined or "фамил" in combined:
        return "name"
    if "город" in combined or "city" in combined:
        return "city"
    if "адрес" in combined or "address" in combined:
        return "address"
    if "коммент" in combined or "comment" in combined or "сообщен" in combined or "message" in combined:
        return "comment"
    return "default"


def get_test_value(
    field_type: str = "default",
    strategy: str = "happy",
) -> str:
    """Получить тестовое значение для поля по типу и стратегии."""
    if strategy == "happy":
        return HAPPY_PATH.get(field_type, HAPPY_PATH["default"])
    elif strategy == "negative":
        pool = NEGATIVE.get(field_type, NEGATIVE["default"])
        return random.choice(pool)
    elif strategy == "boundary":
        pool = BOUNDARY.get(field_type, BOUNDARY["default"])
        return random.choice(pool)
    elif strategy == "security":
        pool = SECURITY.get("default", [])
        return random.choice(pool)
    return HAPPY_PATH.get(field_type, "test")


def get_form_fill_strategy(phase: str, iteration: int) -> str:
    """Выбрать стратегию заполнения по фазе тестирования и номеру итерации."""
    if phase in ("orient", "smoke"):
        return "happy"
    if phase == "critical_path":
        # Чередуем: happy → negative → boundary
        return ["happy", "negative", "boundary"][iteration % 3]
    # exploratory: все стратегии
    return STRATEGIES[iteration % len(STRATEGIES)]


def generate_form_test_data(
    fields: List[Dict],
    strategy: str = "happy",
) -> List[Dict]:
    """
    Для списка полей [{inputType, placeholder, name, ariaLabel}]
    сгенерировать тестовые данные по стратегии.
    """
    result = []
    for f in fields:
        ft = detect_field_type(
            f.get("inputType", ""),
            f.get("placeholder", ""),
            f.get("name", ""),
            f.get("ariaLabel", ""),
        )
        val = get_test_value(ft, strategy)
        result.append({
            "selector": f.get("selector", f.get("name", f.get("placeholder", ""))),
            "value": val,
            "field_type": ft,
            "strategy": strategy,
        })
    return result
