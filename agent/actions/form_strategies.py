"""
Умные стратегии заполнения форм: happy path, negative, boundary, security.
По inputType/placeholder/name генерирует подходящие тестовые данные.
Поддержка RU-полей: ИНН, СНИЛС, паспорт, КПП, ОГРН, дата ДД.ММ.ГГГГ.
"""
import random
from typing import Dict, List, Optional

# Стратегии заполнения
STRATEGIES = ["happy", "negative", "boundary", "security"]


def _inn10_checksum(digits: List[int]) -> int:
    """Контрольная сумма для ИНН 10 цифр (юрлицо)."""
    weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    return sum(d * w for d, w in zip(digits, weights)) % 11 % 10


def _inn12_checksum(digits: List[int]) -> tuple:
    """Контрольные суммы для ИНН 12 цифр (физлицо)."""
    w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c1 = sum(d * w for d, w in zip(digits[:10], w1)) % 11 % 10
    c2 = sum(d * w for d, w in zip(digits[:11], w2)) % 11 % 10
    return c1, c2


def _snils_checksum(digits: List[int]) -> int:
    """Контрольная сумма СНИЛС (последние 2 цифры)."""
    s = sum((i + 1) * d for i, d in enumerate(digits[:9]))
    if s < 100:
        return s
    if s == 100 or s == 101:
        return 0
    s = s % 101
    return 0 if s == 101 else s


def _generate_inn_10() -> str:
    base = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]
    base.append(_inn10_checksum(base))
    return "".join(map(str, base))


def _generate_inn_12() -> str:
    base = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(9)]
    c1, c2 = _inn12_checksum(base)
    base.extend([c1, c2])
    return "".join(map(str, base))


def _generate_snils() -> str:
    base = [random.randint(0, 9) for _ in range(9)]
    rest = _snils_checksum(base)
    d1, d2 = rest // 10, rest % 10
    return "".join(map(str, base)) + f"{d1:02d}"


def _generate_passport_series() -> str:
    return f"{random.randint(10, 99)} {random.randint(10, 99)}"


def _generate_passport_number() -> str:
    return f"{random.randint(100000, 999999)}"


def _generate_ogrn_13() -> str:
    base = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(11)]
    c = (int("".join(map(str, base))) % 11) % 10
    return "".join(map(str, base)) + str(c)


def _generate_kpp() -> str:
    return f"{random.randint(100, 999)}{random.randint(10, 99)}{random.randint(1, 9)}{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}"


def _generate_date_ru() -> str:
    return f"{random.randint(1, 28):02d}.{random.randint(1, 12):02d}.{random.randint(1980, 2010)}"


# Тестовые данные по типу поля (happy path)
def _happy_ru(field_type: str) -> str:
    if field_type == "inn":
        return _generate_inn_12() if random.choice([True, False]) else _generate_inn_10()
    if field_type == "snils":
        return _generate_snils()
    if field_type == "passport_series":
        return _generate_passport_series()
    if field_type == "passport_number":
        return _generate_passport_number()
    if field_type == "ogrn":
        return _generate_ogrn_13()
    if field_type == "kpp":
        return _generate_kpp()
    if field_type == "date_ru":
        return _generate_date_ru()
    return ""


HAPPY_PATH_BASE = {
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

HAPPY_PATH = dict(HAPPY_PATH_BASE)
# RU-поля заполняются генераторами при первом обращении
HAPPY_PATH["inn"] = ""
HAPPY_PATH["snils"] = ""
HAPPY_PATH["passport_series"] = ""
HAPPY_PATH["passport_number"] = ""
HAPPY_PATH["ogrn"] = ""
HAPPY_PATH["kpp"] = ""
HAPPY_PATH["date_ru"] = ""

NEGATIVE = {
    "email": ["test", "@", "test@", "@test.com", "test@@test.com", "тест@тест.рф", ""],
    "tel": ["abc", "123", "+7", "++79991234567", "0", ""],
    "phone": ["abc", "123", "+7", "++79991234567", ""],
    "password": ["1", "abc", "", "   "],
    "number": ["abc", "-1", "0", "99999999", "3.14", "", "NaN"],
    "url": ["http", "://", "not-a-url", "ftp://test", ""],
    "date": ["2025-13-32", "abc", "0000-00-00", ""],
    "text": ["", "   ", "a", "<script>alert(1)</script>"],
    "inn": ["", "123", "12345678901", "1234567890123", "abc"],
    "snils": ["", "123", "123456789", "123456789012"],
    "ogrn": ["", "123", "12345678901234"],
    "kpp": ["", "123", "123456789"],
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
    # RU-поля
    if "инн" in combined or "inn" in combined:
        return "inn"
    if "снилс" in combined or "snils" in combined:
        return "snils"
    if "паспорт" in combined and ("сери" in combined or "series" in combined):
        return "passport_series"
    if "паспорт" in combined and ("номер" in combined or "number" in combined or "№" in combined):
        return "passport_number"
    if "огрн" in combined or "ogrn" in combined:
        return "ogrn"
    if "кпп" in combined or "kpp" in combined:
        return "kpp"
    if ("дата" in combined or "birth" in combined or "рожд" in combined) and ("дд" in combined or "день" in combined or not input_type):
        return "date_ru"
    # Остальные
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
        val = HAPPY_PATH.get(field_type, HAPPY_PATH["default"])
        if val == "" and field_type in ("inn", "snils", "passport_series", "passport_number", "ogrn", "kpp", "date_ru"):
            return _happy_ru(field_type)
        return val
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
