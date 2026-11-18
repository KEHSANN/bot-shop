# Architecture Contracts — Telegram Digital Shop Bot

This document defines the exact cross-module interfaces, DTOs, database relationships, callback factories, FSM states, i18n keys, and middleware contracts shared across Wave 1 and Wave 2.

---

## 1. Layered Module Architecture
```text
app/
├── main.py                      # Unified FastAPI + aiogram Bot runner (webhook & polling)
├── config.py                    # pydantic-settings configuration & secret validation
├── core/
│   ├── constants.py             # 12 languages, dual color palette, domain enums, main menu rules
│   ├── exceptions.py            # Domain exception hierarchy (ShopBotError subclasses)
│   ├── logging.py               # Structured logging with SecretMaskingFilter
│   └── security.py              # Fernet, bcrypt, PyJWT, CSRF, Telegram Login Widget, TOTP, HTML escape, magic-byte file validator
├── database/
│   ├── base.py                  # SQLAlchemy 2.0 DeclarativeBase + naming convention + TimestampMixin
│   ├── session.py               # AsyncEngine + async_session_factory + dependency helper
│   ├── models.py                # 17 PostgreSQL tables with constraints & relationships
│   └── seed.py                  # Idempotent seed (12-lang translations, 5 main buttons, settings, sample catalog)
├── locales/
│   ├── seed_translations.py     # Complete 12-language translation dictionary (no placeholders)
│   └── i18n.py                  # TranslationService with Redis cache + PostgreSQL fallback & invalidation
├── services/
│   ├── dtos.py                  # Pydantic v2 DTOs (Decimal-only money)
│   ├── interfaces.py            # @runtime_checkable Service Protocols
│   ├── user_service.py          # [Wave 2: bot-customer]
│   ├── support_service.py       # [Wave 2: bot-customer]
│   ├── shop_service.py          # [Wave 2: shop-engine]
│   ├── cms_service.py           # [Wave 2: shop-engine]
│   ├── analytics_service.py     # [Wave 2: shop-engine]
│   ├── payment_service.py       # [Wave 2: payment-engine]
│   ├── gateways/                # [Wave 2: payment-engine]
│   └── broadcast_service.py     # [Wave 2: bot-admin]
├── bot/
│   ├── dispatcher.py            # [Wave 2: bot-customer]
│   ├── states.py                # [Wave 2: bot-customer]
│   ├── middlewares/             # [Wave 2: bot-customer]
│   ├── keyboards/               # [Wave 2: bot-customer, shop-engine, payment-engine, bot-admin]
│   └── handlers/                # [Wave 2: bot-customer, shop-engine, payment-engine, bot-admin]
└── web_admin/
    ├── app.py                   # [Wave 2: web-admin]
    ├── deps.py                  # [Wave 2: web-admin]
    ├── webhooks.py              # [Wave 2: payment-engine]
    ├── routes/                  # [Wave 2: web-admin]
    ├── templates/               # [Wave 2: web-admin]
    └── static/                  # [Wave 2: web-admin]
```

---

## 2. Database Schema & Key Relationships (`app/database/models.py`)
1. `users` (`User`): `telegram_id` (`BIGINT`, unique indexed), `language_code` (`en` default), `balance_usd` (`NUMERIC(18,8) >= 0`), `balance_irt` (`NUMERIC(18,2) >= 0`), `is_blocked`, `is_admin`.
2. `wallet_ledger` (`WalletLedger`): Immutable append-only audit log (`user_id` FK `RESTRICT`, `currency`, `before_balance`, `change_amount`, `after_balance` all `NUMERIC(18,8)`, `reference_type`, `reference_id`, `admin_id`, `note`, `created_at`).
3. `categories` (`Category`): `slug` (unique), `sort_order`, `is_active`, `color_emoji`, `color_hex`, `names_json` (`JSONB`), `descriptions_json` (`JSONB`).
4. `products` (`Product`): `category_id` FK `CASCADE`, `slug` (unique), `delivery_type` (`auto`|`manual`), `manual_prompt_key`, `price_usd` (`NUMERIC(18,8)`), `price_irt` (`NUMERIC(18,2)` nullable), `color_emoji`, `color_hex`, `is_pinned`, `is_active`, `sort_order`, `manual_stock_count` (`-1` = unlimited for manual), `low_stock_threshold`, `sold_count`, `titles_json`, `descriptions_json`, `rules_json`, `manual_prompts_json`.
5. `product_bulk_tiers` (`ProductBulkTier`): `product_id` FK `CASCADE`, `min_qty >= 2`, `unit_price_usd` (`NUMERIC(18,8)`), `unit_price_irt` (`NUMERIC(18,2)` nullable), unique `(product_id, min_qty)`.
6. `product_stock` (`ProductStock`): `product_id` FK `CASCADE`, `encrypted_payload` (Fernet), `payload_hash`, `status` (`available`|`reserved`|`delivered`), `order_id` FK `SET NULL`, `created_at`, `delivered_at`. Composite index `(product_id, status, id)`.
7. `orders` (`Order`) & `order_items` (`OrderItem`): `order_number` (unique), `user_id` FK `RESTRICT`, `product_id` FK `RESTRICT`, `quantity`, `currency`, `unit_price`, `total_amount` (`NUMERIC(18,8)`), `status` (`pending_input`|`pending_fulfillment`|`completed`|`cancelled`|`refunded`), `fulfillment_input`, `fulfillment_note`, `fulfilled_by_admin_id`, `fulfilled_at`. `OrderItem` preserves `stock_id` and `encrypted_delivered_payload`.
8. `crypto_wallets` (`CryptoWallet`): `coin_name`, `symbol`, `network`, `wallet_address`, `qr_image_path`, `min_deposit_usd` (`NUMERIC(18,8)`), `fee_note`, `instructions`, `instructions_json` (`JSONB`), `verification_mode`, `explorer_config` (`JSONB`), `is_active`, `sort_order`.
9. `fiat_payment_methods` (`FiatPaymentMethod`): `method_type` (`card_to_card`|`gateway`), `title`, `card_number`, `sheba`, `cardholder_name`, `bank_name`, `gateway_provider`, `gateway_merchant_id_encrypted` (Fernet), `min_deposit_irt` (`NUMERIC(18,2)`), `instructions`, `is_active`, `sort_order`.
10. `payments` (`Payment`): `user_id` FK `RESTRICT`, `payment_category` (`crypto`|`fiat_card`|`fiat_gateway`), `currency`, `amount_usd` (`NUMERIC(18,8)`), `amount_irt` (`NUMERIC(18,2)`), `exchange_rate_used` (`NUMERIC(18,4)`), `crypto_wallet_id`, `fiat_method_id`, `tx_hash` (`UNIQUE` nullable), `authority_track_id` (`UNIQUE` nullable), `receipt_file_id`, `receipt_local_path`, `receipt_note`, `status` (`pending`|`approved`|`rejected`|`expired`), `admin_id`, `admin_note`, `created_at`, `reviewed_at`.
11. `support_tickets` (`SupportTicket`) & `support_messages` (`SupportMessage`): `ticket_number` (unique), `user_id` FK `RESTRICT`, `category` (`deposit_issue`|`other_issue`), `subject`, `status` (`open`|`answered`|`closed`), messages with `sender_role` (`user`|`admin`), `sender_telegram_id`, `message_text`, `telegram_file_id`, `file_type`.
12. `ui_translations` (`UITranslation`): `(key, lang_code)` unique, `text_value`.
13. `ui_buttons` (`UIButton`): `button_key` (unique), `menu_scope`, `row_index`, `col_index`, `color_emoji`, `color_hex`, `is_active`, `labels_json`.
14. `app_settings` (`AppSetting`): `key` PK, `value`, `description`, `updated_at`.
15. `broadcast_jobs` (`BroadcastJob`): `admin_id`, `target_filter`, `message_payload` (`JSONB`), `total_targets`, `sent_count`, `failed_count`, `status`, `created_at`, `completed_at`.

---

## 3. Service Protocols & DTO Contracts
All Wave 2 services MUST implement the `@runtime_checkable` protocols in `app/services/interfaces.py` and exchange Pydantic DTOs from `app/services/dtos.py`:
- `UserServiceProtocol` (`app/services/user_service.py`)
- `ShopServiceProtocol` (`app/services/shop_service.py`)
- `PaymentServiceProtocol` (`app/services/payment_service.py`)
- `SupportServiceProtocol` (`app/services/support_service.py`)
- `CMSServiceProtocol` (`app/services/cms_service.py`)
- `BroadcastServiceProtocol` (`app/services/broadcast_service.py`)
- `AnalyticsServiceProtocol` (`app/services/analytics_service.py`)
- `TranslationService` (`app/locales/i18n.py`, already implemented in Wave 1)

---

## 4. Bot CallbackData Factories (`aiogram.filters.callback_data.CallbackData`)
All callback strings MUST stay safely under Telegram's **64-byte limit**. Standard contracts:

| Class | Prefix | Fields & Values | Owner |
| :--- | :--- | :--- | :--- |
| `MenuCB` | `menu` | `action: str` (`main`, `shop`, `deposit`, `balance`, `support`, `lang`, `orders`, `noop`) | `bot-customer` |
| `LangCB` | `lang` | `code: str` (`fa`, `en`, `ar`, `hi`, `ru`, `de`, `zh`, `es`, `pt`, `id`, `fr`, `tr`) | `bot-customer` |
| `CatCB` | `cat` | `id: int`, `page: int = 0` | `shop-engine` |
| `ProdCB` | `prod` | `id: int`, `action: str = "view"` (`view`, `qty`, `buy_usd`, `buy_irt`, `custom_qty`) | `shop-engine` |
| `QtyCB` | `qty` | `prod_id: int`, `qty: int` | `shop-engine` |
| `OrderCB` | `ord` | `id: int`, `action: str = "view"` (`view`, `list`) | `shop-engine` / `bot-customer` |
| `DepCB` | `dep` | `kind: str`, `id: int = 0`, `action: str = "select"` (`kind`: `crypto`\|`fiat`; `action`: `select`\|`pay`\|`cancel`) | `payment-engine` |
| `PayCB` | `pay` | `id: int`, `action: str` (`approve`, `reject`, `view`) | `payment-engine` / `bot-admin` |
| `SupCB` | `sup` | `action: str`, `id: int = 0`, `cat: str = ""` (`new`, `cat`, `view`, `reply`, `close`) | `bot-customer` / `bot-admin` |
| `AdmCB` | `adm` | `sec: str`, `act: str = "list"`, `id: int = 0`, `page: int = 0` | `bot-admin` |

*Recommendation:* Define all shared `CallbackData` classes in `app/bot/keyboards/common_kb.py` so cross-module handlers (e.g. `bot-admin` handling `PayCB` or `SupCB` notifications) parse identical schemas.

---

## 5. FSM States Contract (`app/bot/states.py`)
All FSM states use `RedisStorage` with key isolation per `(bot_id, chat_id, user_id)`:
- `ShopStates`: `waiting_custom_qty`, `waiting_manual_input`
- `DepositStates`: `waiting_crypto_amount`, `waiting_crypto_txid_or_receipt`, `waiting_fiat_amount`, `waiting_fiat_receipt`
- `SupportStates`: `waiting_ticket_message`, `waiting_reply_message`
- `AdminStates`: `waiting_user_search`, `waiting_balance_amount`, `waiting_balance_note`, `waiting_direct_message`, `waiting_reject_reason`, `waiting_fulfill_note`, `waiting_category_input`, `waiting_product_input`, `waiting_stock_upload`, `waiting_wallet_input`, `waiting_fiat_input`, `waiting_cms_text`, `waiting_broadcast_content`, `waiting_support_reply`, `waiting_exchange_rate`

---

## 6. Middleware Execution Order
1. `LoggingMiddleware` (outer update middleware: correlation ID, sanitized log)
2. `BanCheckMiddleware` (outer update middleware: drops all updates from `is_blocked == True` users immediately)
3. `RateLimitMiddleware` (outer update middleware: Redis sliding window `RATE_LIMIT_MAX_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`; emits localized `err.rate_limited` alert)
4. `DbSessionMiddleware` / `AuthContextMiddleware` (inner middleware: opens `AsyncSession`, upserts/loads `UserDTO`, resolves `is_admin`)
5. `I18nMiddleware` (inner middleware: binds `TranslationService` and user `lang_code` into handler data)
6. `AdminAuthMiddleware` (admin router guard: enforces `telegram_id in settings.admin_ids or user.is_admin`)

---

## 7. Region-Aware Payment & Main Menu Rules
1. **Main Menu Rule:** `TranslationService.get_main_menu_buttons(lang_code)` returns ONLY the 5 core buttons (`shop`, `deposit`, `balance`, `support`, `language`). Admin-pinned products (`Product.is_pinned == True` and `is_active == True`, capped by `AppSetting["pinned_limit"]`) are prepended above Row 1.
2. **Region Routing Rule:** `PaymentService.get_available_methods_for_language(lang_code)`:
   - If `lang_code == "fa"`: returns active `FiatPaymentMethod` rows AND active `CryptoWallet` rows.
   - For ALL other 11 languages (`en`, `ar`, `hi`, `ru`, `de`, `zh`, `es`, `pt`, `id`, `fr`, `tr`): returns **ONLY** active `CryptoWallet` rows (`fiat_methods=[]`).

---

## 8. Error Handling & Response Conventions
- Domain operations raise subclasses of `ShopBotError` (`app.core.exceptions`):
  - Bot handlers catch `ShopBotError` and present a localized, HTML-escaped user-friendly message or `callback_query.answer(..., show_alert=True)`.
  - FastAPI routes map `ShopBotError` subclasses to explicit HTTP status codes (`400` validation, `401` auth, `403` authorization, `404` not found, `409` duplicate TXID/state conflict, `429` rate limit).
- All user-visible strings sent with `ParseMode.HTML` MUST escape untrusted fields using `app.core.security.escape_html`.
