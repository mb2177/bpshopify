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

# Домен для HTTPS: если SITE_ADDRESS не задан, берём его из SITE_URL (https://домен)
SITE_ADDRESS_VAL=$(grep -E '^SITE_ADDRESS=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]')
SITE_URL_VAL=$(grep -E '^SITE_URL=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]')
if [ -z "$SITE_ADDRESS_VAL" ] && [[ "$SITE_URL_VAL" == https://* ]]; then
  SITE_ADDRESS_VAL=$(echo "$SITE_URL_VAL" | sed -E 's#^https://##; s#/.*$##')
  sed -i '/^SITE_ADDRESS=/d' .env
  echo "SITE_ADDRESS=$SITE_ADDRESS_VAL" >> .env
fi
SITE_ADDRESS_VAL=$(echo "$SITE_ADDRESS_VAL" | sed -E 's#^https?://##; s#/.*$##')
sed -i '/^SITE_ADDRESS=/d' .env && echo "SITE_ADDRESS=$SITE_ADDRESS_VAL" >> .env
export SITE_ADDRESS="$SITE_ADDRESS_VAL"   # переменная окружения сессии важнее .env для docker compose
echo "== Домен для HTTPS: ${SITE_ADDRESS_VAL:-не задан (только http)}"
echo "== SITE_URL начинается с https: $([[ "$SITE_URL_VAL" == https://* ]] && echo да || echo нет)"

docker compose up -d --build --remove-orphans
docker compose up -d --force-recreate --no-deps caddy
docker image prune -f >/dev/null 2>&1 || true
sleep 5
docker compose ps
echo "== Проверка сайта"
curl -fsS -o /dev/null -w "HTTP %{http_code}\n" -H "Host: ${SITE_ADDRESS_VAL:-localhost}" http://127.0.0.1/login || (docker compose logs --tail=80; exit 1)
docker compose exec -T web python -m app.smoke
sleep 20
echo "== Caddy (сертификат):"
docker compose logs --tail=25 caddy | grep -iE "certificate|obtain|error|tls" | tail -10 || true

