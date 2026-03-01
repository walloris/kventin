# Установка MTProxy для Telegram

## ⚠️ Важно: безопасность

**Ты указал пароль от root в открытом виде.** После настройки обязательно смени пароль:
```bash
passwd root
```

---

## Быстрая установка (рекомендуется)

### 1. Подключись к серверу

```bash
ssh root@43.245.224.56
```

### 2. Запусти автоматический установщик

```bash
curl -L -o mtp_install.sh https://git.io/fj5ru && bash mtp_install.sh
```

Скрипт задаст несколько вопросов — можно везде жать Enter (по умолчанию всё настроено).

### 3. Или полностью автоматическая установка (без вопросов)

```bash
curl -L -o mtp_install.sh https://git.io/fj5ru && \
SECRET=$(head -c 16 /dev/urandom | xxd -ps) && \
bash mtp_install.sh -p 443 -s $SECRET -t 8b081275ec12abd306faeb2f13efbdcb -a dd -a tls -d s3.amazonaws.com && \
echo "Твой секрет: $SECRET"
```

**Сохрани выведенный секрет** — он понадобится для ссылки proxy.

### 4. Открой порт в файрволе (если используется ufw)

```bash
ufw allow 443/tcp
ufw reload
```

### 5. Готовая ссылка для Telegram

После установки скрипт выведет ссылки. Формат:

```
https://t.me/proxy?server=43.245.224.56&port=443&secret=ТВОЙ_СЕКРЕТ
```

В Telegram: Настройки → Данные и память → Прокси → Добавить прокси → вставь ссылку.

---

## Альтернатива: классический MTProxy (GetPageSpeed)

Если нужен более простой вариант без Erlang:

```bash
# Установка зависимостей
apt update && apt install -y git curl build-essential libssl-dev zlib1g-dev

# Сборка
git clone https://github.com/GetPageSpeed/MTProxy
cd MTProxy
echo '-fcommon' >> Makefile  # добавить в COMMON_CFLAGS и COMMON_LDFLAGS
make

# Настройка
mkdir -p /opt/MTProxy
cp objs/bin/mtproto-proxy /opt/MTProxy/
cd /opt/MTProxy
curl -s https://core.telegram.org/getProxySecret -o proxy-secret
curl -s https://core.telegram.org/getProxyConfig -o proxy-multi.conf

# Генерация секрета
SECRET=$(head -c 16 /dev/urandom | xxd -ps)
echo "Секрет: $SECRET"

# Запуск (порт 8443)
./mtproto-proxy -u nobody -p 8888 -H 8443 -S $SECRET --aes-pwd proxy-secret proxy-multi.conf -M 1
```

---

## Проверка работы

```bash
# Статус сервиса (для seriyps)
systemctl status mtproto-proxy

# Логи
tail -f /var/log/mtproto-proxy/application.log
```
