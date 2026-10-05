#!/usr/bin/env bash
# ==============================================================================
# deploy/deploy.sh — Automated Production Deployment Script from GitHub on Linux
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/chronomed"
WEB_ROOT="/var/www/medical-app"
SQLITE_DATA_DIR="${APP_DIR}/data/sqlite"
CONTAINER_UID=10001
CONTAINER_GID=10001

echo "==> [1/6] Pulling latest code from GitHub..."
cd "${APP_DIR}"
git fetch --all
git reset --hard origin/main

echo "==> [2/6] Preparing SQLite WAL directory & permissions (UID ${CONTAINER_UID})..."
sudo mkdir -p "${SQLITE_DATA_DIR}"
sudo chown -R "${CONTAINER_UID}:${CONTAINER_GID}" "${SQLITE_DATA_DIR}"
sudo chmod 2770 "${SQLITE_DATA_DIR}"
sudo find "${SQLITE_DATA_DIR}" -type f -exec chmod 660 {} +

echo "==> [3/6] Building & restarting FastAPI Docker container..."
docker compose up -d --build --remove-orphans

echo "==> [4/6] Building Vue 3 PWA frontend bundle..."
cd "${APP_DIR}/frontend"
if [ -f package-lock.json ]; then npm ci; else npm install; fi
npm run build

echo "==> [5/6] Syncing compiled frontend to ${WEB_ROOT}..."
sudo mkdir -p "${WEB_ROOT}"
sudo rsync -av --delete "${APP_DIR}/frontend/dist/" "${WEB_ROOT}/"
sudo chown -R caddy:caddy "${WEB_ROOT}"
sudo chmod -R 755 "${WEB_ROOT}"

echo "==> [6/6] Validating & reloading Host Caddy..."
sudo mkdir -p /var/log/caddy
sudo touch /var/log/caddy/chronomed-access.log
sudo chown -R caddy:caddy /var/log/caddy
sudo chmod 755 /var/log/caddy
sudo chmod 644 /var/log/caddy/chronomed-access.log
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy

echo "==> Deployment complete! Checking backend health..."
curl -fsS http://127.0.0.1:8000/api/v1/health && echo ""
