#!/usr/bin/env bash
# Обновление MaxBridge на сервере одной командой:
#
#   bash /root/maxbridge/deploy/update.sh
#
# Забирает свежий код из git, доставляет зависимости и перезапускает сервис.
# .env, data/ и сессия MAX не трогаются — они вне репозитория.
set -euo pipefail

DIR="${MAXBRIDGE_DIR:-/root/maxbridge}"
cd "$DIR"

echo "==> было: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

git pull --ff-only
.venv/bin/pip install --quiet -r requirements.txt

echo "==> стало: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

if systemctl is-enabled --quiet maxbridge 2>/dev/null; then
    systemctl restart maxbridge
    sleep 2
    systemctl is-active --quiet maxbridge \
        && echo "==> сервис перезапущен и работает" \
        || { echo "==> СЕРВИС НЕ ПОДНЯЛСЯ, смотри: journalctl -u maxbridge -n 50"; exit 1; }
else
    echo "==> сервис не установлен в systemd, перезапускать нечего"
fi
