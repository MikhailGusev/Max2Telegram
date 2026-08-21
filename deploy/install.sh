#!/usr/bin/env bash
# Первая установка MaxBridge на сервер. Запускать от root:
#
#   curl -fsSL https://raw.githubusercontent.com/MikhailGusev/maxbridge/main/deploy/install.sh | bash
#
# Или вручную: склонировать репозиторий и выполнить bash deploy/install.sh
set -euo pipefail

REPO="${MAXBRIDGE_REPO:-https://github.com/MikhailGusev/maxbridge.git}"
DIR="${MAXBRIDGE_DIR:-/root/maxbridge}"

echo "==> MaxBridge: установка в $DIR"

if ! command -v python3 >/dev/null; then
    echo "Нет python3. Поставь: apt install -y python3 python3-venv" >&2
    exit 1
fi

if [ -d "$DIR/.git" ]; then
    echo "==> репозиторий уже есть, обновляю"
    git -C "$DIR" pull --ff-only
else
    git clone "$REPO" "$DIR"
fi

cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# .env и data/ в git не хранятся: секреты и переписка остаются только здесь
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "==> создан .env — заполни его перед запуском"
fi
mkdir -p data && chmod 700 data

if [ ! -f /etc/systemd/system/maxbridge.service ]; then
    cp deploy/maxbridge.service /etc/systemd/system/
    systemctl daemon-reload
    echo "==> systemd-юнит установлен (не запущен)"
fi

echo
echo "Готово. Дальше:"
echo "  1) nano $DIR/.env                      — токены и id владельца"
echo "  2) скопировать data/max_session.json   — сессию MAX с рабочей машины"
echo "  3) $DIR/.venv/bin/python -m maxbridge check"
echo "  4) systemctl enable --now maxbridge"
