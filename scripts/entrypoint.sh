#!/usr/bin/env sh
set -eu

echo "[entrypoint] Applying Alembic database migrations..."
alembic upgrade head

echo "[entrypoint] Launching unified FastAPI + aiogram server..."
exec python -m app.main
