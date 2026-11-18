# Multi-Language Telegram Digital Shop Bot & Admin Suite

Production-grade asynchronous **Telegram Digital Goods Shop Bot** (`aiogram 3.x`), **Web Admin CMS Dashboard** (`FastAPI` + `Jinja2` + Tailwind CSS), and **Telegram Mini App** (`/miniapp`), backed by **PostgreSQL 16**, **Redis 7**, and **Nginx**.

---

## Key Capabilities

- **Strictly Scoped 5-Button Main Menu (`/start`):** Single-page-app (`edit_text`) UX featuring admin-pinned featured products above Row 1 followed exclusively by `🛒 Shop`, `💳 Deposit`, `💰 Balance`, `🆘 Support`, and `🌐 Language` (zero extra clutter buttons).
- **12-Language Engine & Dynamic CMS:** Complete native localization dictionaries + RTL formatting (`fa`, `ar`) across `fa, en, ar, hi, ru, de, zh, es, pt, id, fr, tr`, persisted in `ui_translations` and `ui_buttons` with Redis caching and instant cache invalidation.
- **Dual-Layer Color System:**
  - *Layer 1 (Telegram Bot API):* Customizable emoji badges (`🟢🔴🟡🔵🟣🟠⚫⚪`) on menu buttons, categories, and product stock buttons.
  - *Layer 2 (Web Admin & Mini App):* Live `#RRGGBB` / RGB CSS color picker rendering colored cards, badges, and buttons in real time.
- **Shop, Bulk Tiers & Atomic Delivery:**
  - Auto-delivery keys/accounts encrypted at rest with **Fernet (AES-128-CBC + HMAC-SHA256)** and allocated atomically via PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED`.
  - Quantity-based bulk discount tiers (`min_qty -> discounted unit price`) computed strictly in `Decimal` / `NUMERIC(18, 8)`.
  - Manual activation/service products (`pending_input` -> `pending_fulfillment` -> `completed` or atomic `refunded`).
- **Region-Aware Deposit & Payment Routing:**
  - **Persian (`fa`):** Shows **both** Iranian Rial/Toman methods (Card-to-Card with receipt upload + ZarinPal/Zibal/NextPay/IDPay gateway hooks) **and** Cryptocurrency wallets.
  - **All Other 11 Languages:** Shows **strictly Cryptocurrency wallets** (`fiat_methods = []`), supporting unlimited admin-configured coins/networks, custom or auto-generated QR codes, TXID/receipt review, and TronGrid/BscScan/NOWPayments/Oxapay adapters.
- **Dual Admin Control (In-Bot `/admin` + Web Dashboard `/admin`):**
  - Analytics KPIs & low-stock alerts, user search (`telegram_id` / `@username` / name), atomic USD/IRT balance charge/deduct, ban/unblock, direct messaging, rate-limited mass broadcast (`~25 msg/s` with `FloodWait` retry), catalog/color/stock manager, payments approval queue, two-way support ticket chat, and runtime CMS editor.
- **Hardened Security:** Outermost ban & Redis sliding-window rate-limit middlewares, `ADMIN_TELEGRAM_IDS`/DB RBAC, `JWT + Bcrypt + HMAC CSRF + TOTP 2FA + Telegram Login Widget`, Mini App `initData` HMAC, `UNIQUE` replay constraints on `tx_hash`/`authority_track_id`, immutable `wallet_ledger`, magic-byte `<= 5MB` upload validation, `html.escape`, and constant-time `X-Telegram-Bot-Api-Secret-Token` webhook verification.

---

## Directory Structure

```text
telegram-shop-bot/
├── docker-compose.yml            # postgres:16-alpine, redis:7-alpine, app, nginx:alpine
├── Dockerfile                    # Slim Python 3.11 non-root container
├── .env.example                  # Complete environment configuration template
├── alembic.ini & migrations/     # Async Alembic schema migrations (17 tables)
├── nginx/nginx.conf              # Reverse proxy + Webhook secret header + TLS template
├── scripts/                      # entrypoint.sh, deploy.sh, backup_db.sh, gen_secrets.py
├── app/
│   ├── main.py                   # Unified FastAPI + aiogram Bot runner (webhook/polling)
│   ├── config.py                 # pydantic-settings strict configuration
│   ├── core/                     # security.py, constants.py, exceptions.py, logging.py
│   ├── database/                 # Async SQLAlchemy 2.0 models, session, idempotent seed
│   ├── locales/                  # 12-language dictionary seed & Redis i18n cache service
│   ├── services/                 # User, Shop, Payment, Support, CMS, Analytics, Broadcast + gateways/
│   ├── bot/                      # Dispatcher, middlewares, states, keyboards, customer & /admin handlers
│   └── web_admin/                # FastAPI routes, Jinja2 Tailwind templates, Mini App, payment webhooks
├── docs/                         # MASTER_PROMPT, ARCHITECTURE_CONTRACTS, SECURITY_ASSESSMENT, DEPLOYMENT_GUIDE
└── tests/                        # Foundation, subsystem, E2E integration, and security audit suites
```

---

## Quick Start (Docker Compose)

1. Generate `.env` with strong secrets:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python scripts/gen_secrets.py --write-env --domain shop.example.com --bot-mode webhook
   ```
2. Set `BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`, and `DOMAIN` in `.env`, then deploy:
   ```bash
   ./scripts/deploy.sh
   ```
3. Access services via Nginx:
   - Health probe: `http://127.0.0.1/healthz`
   - Web Admin Login: `http://127.0.0.1/admin/login` (or `/login`)
   - Telegram Mini App: `http://127.0.0.1/miniapp`

---

## Running Tests

Ensure `postgres` and `redis` containers are healthy (`docker compose up -d --wait postgres redis`), migrations and seeds are applied, and run:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.database.seed
.venv/bin/pytest -v
```

---

## Documentation

- [Requirements Checklist](docs/REQUIREMENTS_CHECKLIST.md)
- [Architecture Contracts](docs/ARCHITECTURE_CONTRACTS.md)
- [Coding Standards](docs/CODING_STANDARDS.md)
- [File Ownership Matrix](docs/FILE_OWNERSHIP.md)
- [Security Assessment](docs/SECURITY_ASSESSMENT.md)
- [Deployment Guide](docs/DEPLOYMENT_GUIDE.md)
