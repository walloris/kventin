# web_fetch

Инструмент для извлечения содержимого веб-страниц и API по URL.

**ВАЖНО:** В корпоративной среде `curl` может быть заблокирован политикой безопасности.
Используй Python-версию (`tools/web_fetch.py`), которая безопасна.

## Установка

Скрипты находятся в `tools/`:
- `tools/web_fetch` — BASH wrapper (вызывает Python-версию)
- `tools/web_fetch.py` — Python-версия (корпоративная, безопасная)

## Использование

```bash
# Базовый GET-запрос
./tools/web_fetch https://api.example.com/data

# или напрямую Python-версию
./tools/web_fetch.py https://api.example.com/data

# Сохранение в файл
./tools/web_fetch https://api.example.com/data -o output.json

# Запрос с заголовками
./tools/web_fetch https://api.example.com/data -H "Authorization: Bearer token123" -H "Content-Type: application/json"

# POST-запрос с данными
./tools/web_fetch https://api.example.com/submit -X POST -d '{"key":"value"}'

# Подробный режим (показать заголовки)
./tools/web_fetch https://api.example.com/data -v

# Тихий режим (без прогресса)
./tools/web_fetch https://api.example.com/data -s
```

## Опции

| Опция | Описание |
|-------|----------|
| `-o, --output FILE` | Сохранить результат в файл |
| `-H, --header HEADER` | Добавить HTTP-заголовок (можно указать несколько раз) |
| `-X, --method METHOD` | Указать метод (GET, POST, PUT, DELETE, PATCH) |
| `-d, --data DATA` | Данные для отправки (для POST/PUT/PATCH) |
| `-s, --silent` | Тихий режим (без прогресса) |
| `-v, --verbose` | Подробный режим (показать заголовки и ошибки) |

## Зависимости

- `python3` (обычно предустановлен на macOS и Linux)

## Примеры

```bash
# Получить JSON с публичного API
./tools/web_fetch https://jsonplaceholder.typicode.com/posts/1

# Отправить POST-запрос с JSON
./tools/web_fetch https://httpbin.org/post \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"name":"John","email":"john@example.com"}'

# Сохранить результат в файл
./tools/web_fetch https://api.example.com/users -o users.json
```

## Почему Python вместо curl?

- ✅ `curl` часто заблокирован корпоративной политикой безопасности
- ✅ `python3` обычно разрешён и предустановлен
- ✅ `urllib` (встроенный в Python) не требует дополнительных разрешений
- ✅ Безопаснее для корпоративной среды
