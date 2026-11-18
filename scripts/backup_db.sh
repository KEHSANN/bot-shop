#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env file not found in $ROOT_DIR"
  exit 1
fi

read_env_var() {
  local key="$1"
  local fallback="$2"
  local val
  val="$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- | sed -E "s/^['\"]|['\"]$//g" || true)"
  if [ -n "$val" ]; then
    printf '%s' "$val"
  else
    printf '%s' "$fallback"
  fi
}

POSTGRES_USER="$(read_env_var POSTGRES_USER shopbot)"
POSTGRES_DB="$(read_env_var POSTGRES_DB telegram_shop)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
KEEP_COUNT="${KEEP_COUNT:-14}"

mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$BACKUP_DIR/${POSTGRES_DB}_${TIMESTAMP}.dump"

echo "[backup] Dumping PostgreSQL database '$POSTGRES_DB' to $OUT_FILE..."
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" < /dev/null > "$OUT_FILE"
chmod 600 "$OUT_FILE"

# Retain only the newest KEEP_COUNT dumps
ls -1t "$BACKUP_DIR"/${POSTGRES_DB}_*.dump 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -f

echo "[backup] Backup complete ($(du -h "$OUT_FILE" | cut -f1))."
