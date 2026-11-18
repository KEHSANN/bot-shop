#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example or run scripts/gen_secrets.py --write-env."
  exit 1
fi

if grep -q "CHANGE_ME" .env; then
  echo "ERROR: .env still contains CHANGE_ME placeholder values. Replace all secrets before deploying."
  exit 1
fi

mkdir -p nginx/certs backups uploads

echo "[deploy] Validating docker-compose configuration..."
docker compose config --quiet

echo "[deploy] Building application image..."
docker compose build app

echo "[deploy] Starting PostgreSQL 16 and Redis 7..."
docker compose up -d --wait postgres redis

echo "[deploy] Running Alembic migrations and starting app + nginx..."
docker compose up -d --wait app nginx

echo "[deploy] Checking service status and health endpoint..."
docker compose ps
curl -fsS http://127.0.0.1/healthz
echo ""
echo "[deploy] Deployment completed successfully."
