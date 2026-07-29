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

## Автоматический повторный вход

Новый HAR показывает не refresh-token flow, а полный OIDC-вход через
Kerberos/SPNEGO. Если в текущем терминале доступен действующий Kerberos TGT,
скрипт может при потере cookie-сессии один раз пройти этот вход и повторить
тот же UUID:

```bash
python3 -m pip install -r requirements-addressbook.txt
klist -t

python3 scripts/uuid_to_tubnum.py \
  --input /path/to/addr.csv \
  --request-rtf /path/to/запрос.rtf \
  --output /path/to/uuid_tubnum.csv \
  --limit 10 \
  --rate 1 \
  --auto-reauth
```

На macOS `klist -t` проверяет наличие билета без печати его содержимого. Если
команда завершается с ошибкой, сначала получите Kerberos-билет обычным
корпоративным способом. Пароль, билет и SPNEGO-заголовки скрипт не сохраняет
и не печатает.

Повторный вход запускается в чистой HTTP-сессии. Разрешены только HTTPS и
наблюдавшийся маршрут через `idp02.auth.sigma.sbrf.ru`,
`alt.idp02.auth.sigma.sbrf.ru` и callback Addressbook. Скрипт проверяет
OIDC `state`, сам принимает все `Set-Cookie`, проверяет JSON API и только
после этого повторяет исходный запрос. Больше одного повтора для одного UUID
не выполняется.

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
останавливается. Без `--auto-reauth` сохраните свежий curl в RTF и повторите
ту же команду: существующий результат будет продолжен. Если автоматический
вход не совпал с фактической формой IdP или Kerberos-билет недоступен, скрипт
так же безопасно остановится и предложит этот ручной вариант. Обычные
`Set-Cookie` обновления сохраняются внутри HTTP-сессии автоматически.

Предоставленные ранее `got.mjs` и `cli.mjs` используют другой сервис и
Bearer/refresh-token flow, поэтому их нельзя применять для обновления
cookie-сессии Addressbook.

Последний HAR содержит всю front-channel цепочку: начальный redirect
Addressbook, ответ IdP `WWW-Authenticate: Negotiate`, динамический пустой
POST `login-actions/authenticate`, альтернативный IdP и callback
`/openid-connect-auth/redirect_uri`. Token endpoint, `access_token` и
`refresh_token` в нём отсутствуют.

Экспорт HAR очищен от `Authorization`, `Cookie`, `Set-Cookie` и тел
auth-ответов. Поэтому автоматический вход реализован строго по наблюдавшейся
схеме, включая безопасный разбор auto-submit POST-формы, но окончательно
проверить его можно только в корпоративной сети с доступным Kerberos TGT.
Признаков обязательного клиентского PFX в HAR нет; `--ca-bundle` отвечает
только за доверие серверным сертификатам.
