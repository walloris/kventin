#!/bin/bash
# Быстрая установка MTProxy на сервер 43.245.224.56
# Запускать на сервере: bash install-mtproxy.sh

set -e

echo "=== Установка MTProxy для Telegram ==="

# Скачиваем установщик
curl -L -o mtp_install.sh https://git.io/fj5ru

# Генерируем секрет
SECRET=$(head -c 16 /dev/urandom | xxd -ps)
echo "Сгенерированный секрет: $SECRET"
echo "Сохрани его — понадобится для ссылки!"
echo ""

# Запускаем установку (порт 443, dd+tls для обхода блокировок)
bash mtp_install.sh -p 443 -s "$SECRET" -t 8b081275ec12abd306faeb2f13efbdcb -a dd -a tls -d s3.amazonaws.com

# Открываем порт если ufw установлен
if command -v ufw &> /dev/null; then
    echo ""
    echo "Открываю порт 443 в файрволе..."
    ufw allow 443/tcp 2>/dev/null || true
    ufw reload 2>/dev/null || true
fi

echo ""
echo "=== Готово! ==="
echo ""
echo "Ссылки для Telegram (они также показаны выше):"
echo "Secure (dd): https://t.me/proxy?server=43.245.224.56&port=443&secret=dd$SECRET"
echo ""
echo "Используй ссылку Secure или Fake-TLS из вывода выше — они лучше обходят блокировки."
