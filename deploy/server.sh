#!/usr/bin/env bash
# Запускается на VPS при каждом деплое (из GitHub Actions). Идемпотентен.
set -euo pipefail
APP_DIR=/opt/shopify-bot
cd "$APP_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "== Устанавливаю Docker"
  curl -fsSL https://get.docker.com | sh
fi

# Брандмауэр: только SSH, HTTP, HTTPS
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  yes | ufw enable >/dev/null 2>&1 || true
fi

mkdir -p data
chown -R 1000:1000 data
chmod 600 .env

docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true
sleep 5
docker compose ps
echo "== Проверка сайта"
curl -fsS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1/login || (docker compose logs --tail=80; exit 1)
