# Security Assessment — Telegram Digital Shop Bot

**Assessment Date:** 2026-11-18  
**Target:** `/home/ehsan/telegram-shop-bot` (`master` — Post-Wave 3 Integration)  
**Environment Verified:** Debian 13 (`x86_64`), Docker Compose (`postgres:16-alpine`, `redis:7-alpine`, `python:3.11-slim`, `nginx:alpine`), Python 3.13.5 test runner.

---

## 1. Executive Summary

An independent code, configuration, runtime container, and automated security verification audit was conducted across the unified Telegram Digital Shop Bot, FastAPI Web Admin Panel, Telegram Mini App, Payment Gateway Webhooks, PostgreSQL 16 schema, Redis 7 caching/rate-limiting layer, and Nginx/Docker deployment configuration.

All mandatory security controls specified in `docs/MASTER_PROMPT.md` Section 8 are implemented and verified via automated tests (`tests/test_wave1_foundation.py`, `tests/test_security_audit.py`, `tests/test_integration_e2e.py`) and live Docker/Nginx HTTP smoke tests.

---

## 2. Verified Security Controls

| Control Domain | Implementation & File Evidence | Verification Result |
| :--- | :--- | :--- |
| **Bot Admin RBAC** | `AdminAuthMiddleware` (`app/bot/middlewares/admin_mw.py`), `AdminAccessMiddleware` (`app/bot/handlers/admin_bot.py`), and `PayCB` guard in `app/bot/handlers/deposit.py` enforce `ADMIN_TELEGRAM_IDS` or `users.is_admin`. | Non-admin `/admin`, `AdmCB`, and `PayCB(approve)` calls rejected (`Access denied`), zero DB state mutation. |
| **Blocked User Isolation** | `BanCheckMiddleware` (`app/bot/middlewares/ban_mw.py`) runs at outer update middleware layer before handlers; `AuthContextMiddleware` and `get_miniapp_principal` also enforce `is_blocked`. | Blocked user `/start`, inline callbacks, and Mini App `/miniapp/api/*` requests dropped/rejected (`403`). |
| **Web Admin Authentication** | `app/web_admin/routes/auth.py` & `deps.py`: `hmac.compare_digest` username check, `asyncio.to_thread(verify_password)` bcrypt (`rounds=12`), optional RFC 6238 TOTP 2FA (`verify_totp_code`), Telegram Login Widget HMAC (`verify_telegram_login_widget`), `PyJWT` HS256 in `HttpOnly; SameSite=Lax` cookie + Bearer header. | Unauthenticated `/admin/api/dashboard` returns `401`; wrong password/TOTP returns `401`; non-admin Telegram Login Widget returns `403`. |
| **CSRF Protection** | `require_prelogin_csrf` & `require_admin_csrf` (`app/web_admin/deps.py`) validate session-bound HMAC-SHA256 tokens (`generate_csrf_token` / `verify_csrf_token`) across `POST`/`PUT`/`PATCH`/`DELETE`. | State-changing login and admin mutations without `X-CSRF-Token` / form token rejected with `403`. |
| **Mini App `initData` HMAC** | `get_miniapp_principal` (`app/web_admin/deps.py`) validates `WebAppData` HMAC-SHA256 signature and `auth_date` freshness (`verify_telegram_webapp_init_data`). | Missing or tampered `X-Telegram-Init-Data` rejected with `401`. |
| **Atomic Financial & Stock Locking** | `ShopService.purchase_product` uses `SELECT ... FOR UPDATE` on `users` and `SELECT ... FOR UPDATE SKIP LOCKED` on `product_stock`; `UserService.adjust_balance` and `PaymentService.review_payment` lock `users` / `payments` with `FOR UPDATE`. | Concurrent `asyncio.gather` tests confirm zero double-spend on last stock item and zero double-credit on simultaneous payment approval. |
| **Decimal-Only Money & Immutable Ledger** | All monetary DB columns use `NUMERIC(18,8)` (USD/crypto/ledger), `NUMERIC(18,2)` (IRT), or `NUMERIC(18,4)` (exchange rate); zero `Float`/`REAL` columns; `wallet_ledger` is append-only (`ON DELETE RESTRICT`, no `updated_at`). | Schema introspection and static scans pass; negative balance `CHECK` constraints trigger `IntegrityError`. |
| **Idempotency & Replay Protection** | `UNIQUE` indexes on `payments.tx_hash` and `payments.authority_track_id`; empty strings normalized to `None`; `IntegrityError` mapped to `DuplicateTransactionError` (`409`). | Duplicate TXID and duplicate `authority_track_id` submissions rejected deterministically. |
| **Rate Limiting & Cooldowns** | Redis 7 Lua ZSET sliding window (`RateLimitMiddleware`, default `5 req / 3s`) + `check_action_cooldown` and deposit cooldowns (`DEPOSIT_COOLDOWN_SECONDS`). | Burst updates exceeding threshold blocked by Redis and notified via `err.rate_limited`. |
| **XSS & HTML Injection Prevention** | `escape_html` / `sanitize_username` (`app/core/security.py`) applied to untrusted bot inputs before `ParseMode.HTML`; Jinja2 `select_autoescape(default=True, default_for_string=True)` enabled in `deps.py`. | `<script>alert("xss")</script>` escaped to `&lt;script&gt;` in both admin notifications and `/admin/users` HTML output. |
| **Upload Validation** | `validate_receipt_image` checks `<= 5 MB` limit, JPEG/PNG/WEBP magic bytes (`detect_image_mime`), MIME, and extension match; `validate_stock_upload_file` rejects null bytes/binary and enforces UTF-8 `.txt`/`.csv`. | Spoofed `evil.jpg` (`<html>...`) and `> 5 MB` payloads rejected (`400` / `FileValidationError`). |
| **Encryption & Secrets Hygiene** | Fernet (`AES-128-CBC + HMAC-SHA256`) encrypts `product_stock.encrypted_payload`, `order_items.encrypted_delivered_payload`, and `fiat_payment_methods.gateway_merchant_id_encrypted`; `.env` gitignored (`0600`) and excluded from Docker image (`Dockerfile` copies only `app`, `migrations`, `alembic.ini`, `scripts/entrypoint.sh`); `SecretMaskingFilter` redacts tokens/keys/DB URLs from logs. | Fernet round-trips verified; zero hardcoded secrets or stub markers in source tree. |
| **Webhook & Reverse Proxy Hardening** | `/webhook/telegram` validates `X-Telegram-Bot-Api-Secret-Token` with `hmac.compare_digest`; `postgres` and `redis` bound strictly to `127.0.0.1`; `app` container runs as non-root `appuser`; Nginx enforces security headers and `frame-ancestors` for `/miniapp`/`/webapp`. | Live Nginx `POST /webhook/telegram` without or with wrong secret returns `403`. |

---

## 3. Non-Blocking Observations & Residual Operational Steps

1. **Enable HTTPS/TLS Before Production Traffic:** `nginx/nginx.conf` ships with HTTP (`:80`) active and a complete TLS `443` server block commented out until domain certificates (`./nginx/certs/fullchain.pem`, `./nginx/certs/privkey.pem`) are provisioned. Terminate TLS via Nginx + Let's Encrypt before using real `BOT_TOKEN`, Telegram Login Widget, Telegram Mini App, or Web Admin credentials over the public internet.
2. **Production Credentials & 2FA:** Replace dev credentials before production go-live using `scripts/gen_secrets.py --write-env`, set real `BOT_TOKEN` and `ADMIN_TELEGRAM_IDS`, set `APP_ENV=production`, `DEBUG=false`, and enable `TWO_FACTOR_ENABLED=true` with an authenticator `TOTP_SECRET`.
3. **Key Rotation Policy:** `ENCRYPTION_KEY` protects existing `ProductStock`, `OrderItem`, and gateway merchant IDs at rest; rotating it requires a migration script to decrypt with the old key and re-encrypt with the new key.
4. **Host Firewall & Off-Site Backups:** Restrict VPS inbound ports via `ufw` (`22`, `80`, `443` only; never expose `5432`, `6379`, or `8000` publicly) and schedule `scripts/backup_db.sh` via cron with off-host encrypted archival.
5. **Minor Routing Nuance (Low / Informational):** In `app/bot/dispatcher.py`, `deposit.router` is registered before `admin_bot.router`. Because both define a `PayCB` `approve`/`reject` filter, inline 1-click `[✅ Approve]` / `[❌ Reject]` callbacks hit `deposit.cb_admin_review_deposit_notification` first (which enforces `is_admin` RBAC, runs atomic `review_payment`, and notifies the customer immediately) rather than prompting for a custom rejection note in `admin/payments.py`. Web Admin `/admin/payments/{id}/reject` supports custom admin notes.
