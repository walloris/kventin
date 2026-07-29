# Выгрузка `uuid,tubNum` из Addressbook

Утилита `scripts/uuid_to_tubnum.py` читает UUID из второго столбца CSV,
берёт URL, браузерные заголовки и cookies из сохранённой curl-команды в RTF,
запрашивает `/api/home/empInfoFull` и формирует CSV с колонками
`uuid,tubNum`.

Скрипт не печатает cookies, авторизационные заголовки или тела ошибочных
ответов. HTTPS-проверка не отключается, а cookies разрешено отправлять только
на явно заданный хост. Ответы с несколькими несопоставленными `tubNum`
отклоняются, чтобы не записать табельный номер другой сущности.

## Подготовка

Установите зависимости проекта:

```bash
cd /path/to/kventin
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Проверка без HTTP-запросов

```bash
python3 scripts/uuid_to_tubnum.py \
  --input /path/to/addr.csv \
  --request-rtf /path/to/запрос.rtf \
  --output /path/to/uuid_tubnum.csv \
  --dry-run
```

## Пробная выгрузка

Сначала проверьте несколько UUID:

```bash
python3 scripts/uuid_to_tubnum.py \
  --input /path/to/addr.csv \
  --request-rtf /path/to/запрос.rtf \
  --output /path/to/uuid_tubnum.csv \
  --limit 10 \
  --rate 1
```

После проверки удалите `--limit` и задайте разрешённую для стенда скорость:

```bash
python3 scripts/uuid_to_tubnum.py \
  --input /path/to/addr.csv \
  --request-rtf /path/to/запрос.rtf \
  --output /path/to/uuid_tubnum.csv \
  --rate 5
```

## Корпоративный TLS

Если error CSV содержит `SSLError[CERTIFICATE_VERIFY_FAILED]`, укажите
конкретный доверенный корпоративный CA bundle в PEM-формате:

```bash
python3 scripts/uuid_to_tubnum.py \
  --input /path/to/addr.csv \
  --request-rtf /path/to/запрос.rtf \
  --output /path/to/uuid_tubnum.csv \
  --limit 10 \
  --rate 1 \
  --ca-bundle /path/to/corporate-ca.pem
```

Можно также использовать стандартную переменную `REQUESTS_CA_BUNDLE`.
Проверка TLS намеренно не отключается. Не объединяйте в CA bundle все
сертификаты Keychain: это может превратить недоверенные пользовательские
сертификаты в доверенные корни.

Основной результат содержит только подтверждённые непустые пары
`uuid,tubNum`. Рядом создаётся `uuid_tubnum.errors.csv`. При повторном запуске
готовые UUID пропускаются; отсутствующие или ошибочные ответы можно запросить
снова.

При `401`, `403`, redirect на страницу входа или HTML вместо JSON утилита
останавливается. Сохраните свежий curl в RTF и повторите ту же команду:
существующий результат будет продолжен. Обычные `Set-Cookie` обновления
сохраняются внутри HTTP-сессии автоматически.

Предоставленные ранее `got.mjs` и `cli.mjs` используют другой сервис и
Bearer/refresh-token flow, поэтому их нельзя применять для обновления
cookie-сессии Addressbook.

Снятый HAR подтверждает OIDC-цепочку через `idp02.auth.sigma.sbrf.ru`,
`alt.idp02.auth.sigma.sbrf.ru` и callback
`/openid-connect-auth/redirect_uri`. В нём отсутствуют начальный redirect от
Addressbook и ответ, устанавливающий `PLATFORM_SESSION*`, поэтому полный
автоматический повторный вход по этому логу пока не воспроизводится.
