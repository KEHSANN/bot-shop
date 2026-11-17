# Requirements Checklist — Multi-Language Telegram Digital Shop Bot

Authoritative source: [`docs/MASTER_PROMPT.md`](./MASTER_PROMPT.md).
Every checkbox below represents a mandatory deliverable verified at Wave 1, Wave 2, and Wave 3 gates.

---

## 1. Tech Stack & Infrastructure
- [ ] Python 3.11+ / 3.13 compatible asyncio codebase (zero blocking I/O on event loop).
- [ ] `aiogram 3.x` with Redis FSM storage (`RedisStorage`), custom middlewares, `CallbackData` factories, and in-place editing (`editMessageText` / `editMessageCaption`) on navigation.
- [ ] `FastAPI` + `Jinja2` + responsive Tailwind CSS Web Admin Dashboard + Telegram Mini App Shop view.
- [ ] `PostgreSQL 16` + `SQLAlchemy 2.0` async (`asyncpg`) + `Alembic` async migrations.
- [ ] `Redis 7` for FSM storage, sliding-window rate limiting, and i18n/UI button caching.
- [ ] Dockerized via `Dockerfile` (`python:3.11-slim`) and `docker-compose.yml` (`postgres:16-alpine`, `redis:7-alpine`, `app`, `nginx:alpine`).
- [ ] Webhook mode behind Nginx SSL with `X-Telegram-Bot-Api-Secret-Token` validation AND Polling mode for local/dev without domain.

## 2. Strictly Scoped User Bot UI & Navigation
- [ ] Single-page-app UX: inline callbacks edit the existing message (`editMessageText` / `editMessageCaption`) instead of spamming new messages.
- [ ] Every sub-menu includes a `🔙 Back` button returning cleanly to its exact parent menu.
- [ ] First `/start`: language selection prompt (or auto-detect from Telegram `language_code` with instant switch option).
- [ ] Welcome banner customizable per language via CMS displaying:
  - [ ] Numeric Telegram User ID & `@username`
  - [ ] Current USD balance (`$`)
  - [ ] Current Iranian Toman/Rial balance (`IRT`) ONLY when active language is `fa`
- [ ] **STRICT 5-Button Main Menu (+ Optional Admin-Pinned Featured Products):**
  - [ ] Optional top rows: Admin-pinned featured products (`is_pinned=True`) with color badge.
  - [ ] Row 1 (Full Width): `🛒 Shop`
  - [ ] Row 2 (2 Columns): `💳 Deposit` | `💰 Balance`
  - [ ] Row 3 (2 Columns): `🆘 Support` | `🌐 Language`
  - [ ] **STRICT EXCLUSIONS:** Zero `Referral`, `Status`, `Daily Top Invites`, `Device Verification`, or `API Docs` buttons anywhere on the main menu.

## 3. 12-Language Engine & Dynamic CMS
- [ ] Full native translations (no placeholders) for ALL 12 languages:
  - [ ] `fa` — Persian (فارسی) 🇮🇷 (RTL, enables Rial/Toman + Crypto)
  - [ ] `en` — English 🇺🇸 (Global default)
  - [ ] `ar` — Arabic (العربية) 🇸🇦 (RTL)
  - [ ] `hi` — Hindi (हिन्दी) 🇮🇳
  - [ ] `ru` — Russian (Русский) 🇷🇺
  - [ ] `de` — German (Deutsch) 🇩🇪
  - [ ] `zh` — Chinese Simplified (简体中文) 🇨🇳
  - [ ] `es` — Spanish (Español) 🇪🇸
  - [ ] `pt` — Portuguese Brazil (Português) 🇧🇷
  - [ ] `id` — Indonesian (Bahasa Indonesia) 🇮🇩
  - [ ] `fr` — French (Français) 🇫🇷
  - [ ] `tr` — Turkish (Türkçe) 🇹🇷
- [ ] All texts, welcome banners, prompts, and button labels stored in `ui_translations` and `ui_buttons`.
- [ ] Redis cache for translations & buttons with immediate cache invalidation upon Admin edit.
- [ ] Admin can edit any key/prompt/button label in any of the 12 languages from Web Admin & `/admin` without code changes.

## 4. Dual-Layer Dynamic Color System
- [ ] **Layer 1 (Telegram Bot API):** Assign emoji color indicator (`🟢`, `🔴`, `🟡`, `🔵`, `🟣`, `🟠`, `⚫`, `⚪`, or custom emoji) to any product, category, or menu button; product buttons dynamically display `[color_emoji] [Product Name] - Stock: [Count]`.
- [ ] **Layer 2 (Web Admin & Mini App):** Hex/RGB color picker (`#RRGGBB`, e.g., `#22c55e`, `#ef4444`, `#eab308`, `#3b82f6`, `#a855f7`, `#f97316`, `#1f2937`, `#f8fafc`) stored per product/category/button and rendered directly as CSS button/badge styling.

## 5. Shop, Categories, Bulk Discounts & Fulfillment
- [ ] Categories screen lists active categories + `📜 Order History` + `🔙 Back`.
- [ ] Products screen lists active products in category with color indicator + live available stock count.
- [ ] Product detail card displays localized title, rich description, pre-purchase checklist/rules, unit price (`USD` + `IRT` when `fa`), bulk discount tiers (`min_qty -> discounted unit price`), available stock, and sold count.
- [ ] Quantity selection (`1`, `5`, `10`, `Custom Quantity`) + purchase via USD balance (all users) or IRT balance (`fa` users).
- [ ] **Auto-Delivery:** Encrypted credentials stored in `product_stock`, atomically allocated via `SELECT ... FOR UPDATE SKIP LOCKED`, decrypted on delivery, and saved in `order_items`.
- [ ] **Manual Fulfillment:** Prompts user for required activation input (`manual_prompt_key` / `manual_prompts_json`), sets status `pending_fulfillment`, notifies admins immediately, and notifies user upon admin fulfillment.

## 6. Region-Aware Payment & Deposit System
- [ ] **Strict Language/Region Routing:**
  - [ ] `lang != 'fa'` (`en, ar, hi, ru, de, zh, es, pt, id, fr, tr`): Display **ONLY Cryptocurrency methods**.
  - [ ] `lang == 'fa'`: Display **BOTH Iranian Rial/Toman methods AND Cryptocurrency methods**.
- [ ] **Dynamic Crypto Wallets:**
  - [ ] Unlimited admin-configurable coins, networks, dedicated wallet addresses, optional/auto-generated QR codes, min deposit USD, fee notes, instructions, and verification modes.
  - [ ] Manual/semi-auto TXID + receipt image flow with admin inline `[✅ Approve & Credit]` / `[❌ Reject]`.
  - [ ] Modular blockchain/gateway verification adapter hooks (TronGrid, BscScan, NOWPayments, Oxapay).
- [ ] **Iranian Rial/Toman (`fa` only):**
  - [ ] Card-to-Card configuration (16-digit card number, Sheba IBAN, cardholder name, bank name, instructions) + receipt upload + 1-click admin approve/reject.
  - [ ] Modular Iranian gateway adapter (ZarinPal / Zibal / NextPay / IDPay compatible) with callback verification and configurable USD-to-Toman exchange rate (manual or live rate mode).

## 7. Wallet (`💰 Balance`) & Support (`🆘 Support`)
- [ ] Wallet screen shows USD balance (+ IRT balance for `fa`), recent `wallet_ledger` entries, order history shortcut, and deposit shortcut.
- [ ] Support screen offers `💳 Deposit Issue` and `💬 Other Issue / Live Chat`.
- [ ] Two-way support ticket chat inside Telegram with photo/file attachment forwarding, ticket history, and admin direct reply + close.

## 8. Dual Admin Panel (In-Bot `/admin` + Web Admin Dashboard)
- [ ] Dashboard analytics: total users, active users (24h/7d/30d), revenue (`USD` & `IRT`), pending deposits count, open tickets count, low-stock alerts.
- [ ] Member search by numeric `telegram_id`, `@username`, or first/last name.
- [ ] User inspection card: profile stats, charge/deduct USD/IRT balance (with note + user notification), block/unblock user, direct message (text/photo/file), deposit & order history.
- [ ] Broadcast system: single-user DM + mass broadcast (all users or filtered by language / balance > 0) with async rate-limited queue (~25 msg/s), `FloodWait` handling, and progress tracking.
- [ ] Full CRUD for Categories, Products, Bulk Tiers, Stock (paste lines or `.txt`/`.csv` upload), Color Emoji + Hex, Crypto Wallets, Fiat Methods, Payments Queue, Pending Manual Orders, Support Tickets, CMS Texts & UI Buttons, Exchange Rate & Maintenance Mode.

## 9. Mandatory Security & Reliability Controls
- [ ] Bot admin middleware verifies `telegram_id` against `ADMIN_TELEGRAM_IDS` env config or `users.is_admin`.
- [ ] Web Admin secured with JWT + Bcrypt + CSRF protection + Telegram Login Widget HMAC / optional 2FA.
- [ ] Zero floats for money: `Decimal` in Python and `NUMERIC(18, 8)` / `NUMERIC(18, 2)` in PostgreSQL.
- [ ] `SELECT ... FOR UPDATE` on `users` wallet updates and `SELECT ... FOR UPDATE SKIP LOCKED` on `product_stock`.
- [ ] Immutable `wallet_ledger` recording `before_balance`, `change_amount`, `after_balance`, `reference_type`, `reference_id`, `admin_id`, `note`, `created_at`.
- [ ] `UNIQUE` constraints on `payments.tx_hash` and `payments.authority_track_id` preventing replay/double-credit.
- [ ] Redis sliding-window rate limiter (`5 requests / 3 seconds` + deposit/support cooldowns).
- [ ] Outermost blocked-user middleware dropping all updates from `is_blocked == True`.
- [ ] `html.escape` applied to all user-supplied text before Telegram HTML formatting or web rendering.
- [ ] File upload validation checking MIME type, magic bytes (JPEG/PNG/WEBP), and size `<= 5 MB`.
- [ ] Secrets strictly loaded from `.env` via `pydantic-settings`; delivered credentials & gateway merchant IDs encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
