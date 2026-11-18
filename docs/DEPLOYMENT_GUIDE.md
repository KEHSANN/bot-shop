# Deployment & Operations Guide

This guide covers deploying the **Telegram Digital Shop Bot** (aiogram 3.x Bot + FastAPI Web Admin + Telegram Mini App + PostgreSQL 16 + Redis 7 + Nginx) on Ubuntu 22.04/24.04 or Debian 12/13 using Docker Compose.

---

## 1. Prerequisites

- Linux VPS (Ubuntu 22.04+ or Debian 12/13) with >= 1 vCPU, >= 1 GB RAM, >= 10 GB disk.
- Docker Engine (`24+`) and Docker Compose plugin (`docker compose version` >= `2.20`).
- A domain name pointed to your VPS IP (required for HTTPS Webhook mode, Telegram Login Widget, and Telegram Mini App).
- Telegram Bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram User ID (e.g. from `@userinfobot`).

---

## 2. Initial Server Setup & Secrets Generation

```bash
git clone <your-repo-url> telegram-shop-bot
cd telegram-shop-bot
```

Create `.env` from `.env.example` and generate strong cryptographic secrets (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET_KEY`, `CSRF_SECRET_KEY`, `ADMIN_SECRET_KEY`, `BOT_WEBHOOK_SECRET`, Fernet `ENCRYPTION_KEY`, and bcrypt `WEB_ADMIN_PASSWORD_HASH`):

```bash
# Option A: Using a local Python 3.11+ venv with requirements installed
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/gen_secrets.py --write-env --domain shop.example.com --bot-mode webhook

# Option B: Copy .env.example manually and fill every CHANGE_ME value
cp .env.example .env
chmod 600 .env
```

Edit `.env` to configure your Telegram & deployment values:
- `BOT_TOKEN=<your_botfather_token>`
- `TELEGRAM_BOT_USERNAME=<your_bot_username_without_at>`
- `ADMIN_TELEGRAM_IDS=<your_numeric_telegram_id>` (comma-separated for multiple admins)
- `DOMAIN=<your.domain.com>`
- `BOT_WEBHOOK_URL=https://<your.domain.com>/webhook/telegram`
- `WEBAPP_URL=https://<your.domain.com>/miniapp`
- `MINIAPP_BASE_URL=https://<your.domain.com>/miniapp`
- `APP_ENV=production`
- Optional 2FA: set `TWO_FACTOR_ENABLED=true` and `TOTP_SECRET=<base32_secret>`.

> **Important:** Wrap `WEB_ADMIN_PASSWORD_HASH` in single quotes (`'$2b$12$...'`) inside `.env` so Docker Compose does not treat `$` as variable interpolation.

---

## 3. SSL / TLS Setup (Production Webhook & Mini App)

Telegram Webhooks, Telegram Login Widget, and Telegram Mini App require HTTPS.

1. Obtain certificates (e.g., Certbot standalone before starting Nginx on port 80):
   ```bash
   sudo apt-get update && sudo apt-get install -y certbot
   sudo certbot certonly --standalone -d shop.example.com
   mkdir -p nginx/certs
   sudo cp /etc/letsencrypt/live/shop.example.com/fullchain.pem nginx/certs/fullchain.pem
   sudo cp /etc/letsencrypt/live/shop.example.com/privkey.pem nginx/certs/privkey.pem
   sudo chown "$USER:$USER" nginx/certs/*.pem
   chmod 600 nginx/certs/privkey.pem
   ```
2. Open `nginx/nginx.conf`, uncomment the `listen 443 ssl http2;` server block, set `server_name shop.example.com;`, and optionally redirect HTTP `:80` traffic (except ACME challenges) to `https://$host$request_uri`.

---

## 4. Deploy & Verify

Run the automated deployment script:

```bash
./scripts/deploy.sh
```

`scripts/deploy.sh` performs:
1. Validates `.env` exists and contains no `CHANGE_ME` placeholders.
2. Runs `docker compose build app`.
3. Starts `postgres:16-alpine` and `redis:7-alpine` until healthy.
4. Starts `app` (which automatically executes `alembic upgrade head` via `scripts/entrypoint.sh`, runs idempotent 12-language + 5-button seed in `app.main` lifespan, and registers the Telegram webhook if `BOT_MODE=webhook`) and `nginx:alpine`.
5. Checks `docker compose ps` and `http://127.0.0.1/healthz`.

Verify endpoints:
```bash
curl -fsS http://127.0.0.1/healthz
# Expected: {"status":"ok","database":"ok","redis":"ok","bot_mode":"webhook"}

curl -I http://127.0.0.1/admin/login
curl -I http://127.0.0.1/miniapp
```

---

## 5. Development Mode (`BOT_MODE=polling`)

For local testing without a public domain or SSL certificate:
1. Set `BOT_MODE=polling` in `.env`.
2. Start dependencies (`docker compose up -d --wait postgres redis`), run migrations & seed, and start locally:
   ```bash
   .venv/bin/alembic upgrade head
   .venv/bin/python -m app.database.seed
   .venv/bin/python -m app.main
   ```

---

## 6. Database Backup & Restore

Create a compressed PostgreSQL backup (stored in `backups/` with mode `0600`, retaining the latest 14 dumps):
```bash
./scripts/backup_db.sh
```

Restore a backup:
```bash
set -a; source .env; set +a
cat backups/telegram_shop_YYYYMMDDTHHMMSSZ.dump | docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists
```

---

## 7. Security Checklist Before Go-Live

- [ ] `ufw` enabled allowing only `22`, `80`, `443` (`5432`, `6379`, `8000` remain bound to `127.0.0.1` only).
- [ ] HTTPS active on port `443` with valid TLS certificate.
- [ ] `APP_ENV=production`, `DEBUG=false`, `BOT_MODE=webhook`.
- [ ] `BOT_WEBHOOK_SECRET`, `JWT_SECRET_KEY`, `CSRF_SECRET_KEY`, `ENCRYPTION_KEY`, and DB/Redis passwords randomly generated (`scripts/gen_secrets.py`).
- [ ] `TWO_FACTOR_ENABLED=true` configured for Web Admin login.
- [ ] Automated daily cron configured for `./scripts/backup_db.sh` and backed up off-host.
