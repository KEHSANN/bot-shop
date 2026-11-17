# پرامپت جامع و مهندسی‌شده برای ساخت ربات فروشگاهی تلگرام (نسخه چندزبانه، کریپتو/ریالی و پنل ادمین پیشرفته)

## بخش اول: کالبدشکافی و تحلیل فنی ویدیوی ربات مرجع

بررسی فریم‌به‌فریم ویدیوی ارسالی (`@Verify_pixel_bot` - *Gemini - ChatGPT Bot*) مشخصات معماری و رابط کاربری زیر را نشان می‌دهد:

| ثانیه ویدیو | بخش مورد بررسی در ویدیو | رفتار فنی در ربات مرجع | تغییرات و بهینه‌سازی‌های درخواستی شما |
| :--- | :--- | :--- | :--- |
| **00:00 تا 00:05** | دستور `/start` و منوی اصلی (Main Menu) | نمایش بنر، لینک زیرمجموعه‌گیری، رنک کاربر و ۱۱ دکمه شیشه‌ای (`Shop`, `Deposit`, `Balance`, `Status`, `Support`, `Referral`, `Daily Top Invites`, `Device Verification`, `API Docs`, `Language` و ۲ محصول پین‌شده) | **ساده‌سازی منوی اصلی به ۵ دکمه اصلی:** `🛒 Shop`، `💳 Deposit`، `💰 Balance`، `🆘 Support` و `🌐 Language` (به‌همراه امکان پین کردن محصولات ویژه در بالای منو توسط ادمین) و حذف کامل بخش‌های اضافی (`Referral`, `Status`, `Daily Top Invites`, `Device Verification`, `API Docs`). |
| **00:06 تا 00:11** | فروشگاه (`🛒 Shop`) و دسته‌بندی‌ها | ویرایش درجا (`editMessageText`) به لیست دسته‌بندی‌ها (`Gemini AI`, `CapCut`, `Claude`, `Duolingo`, `ChatGPT`, `Muse AI`, `Order History`, `Back`) و نمایش محصولات هر دسته با نشانگر رنگی `🔴 CDK Gemini Pro... - Stock: 322` | امکان افزودن بی‌نهایت دسته‌بندی و محصول متنوع از پنل ادمین، مدیریت موجودی (اتوماتیک/دستی) و **تعیین رنگ اختصاصی برای هر محصول و هر دکمه** (هم با نشانگرهای رنگی تلگرام `🟢/🔴/🟡/🔵/🟣/🟠` روی دکمه‌های شیشه‌ای و هم رنگ پس‌زمینه CSS در نسخه Mini App / Web). |
| **00:12 تا 00:18** | صفحه جزئیات محصول (Product Card) | نمایش عنوان، قوانین/چک‌لیست قبل از خرید، قیمت چندارزی (`VND`, `USD`, `Credit`)، جدول تخفیف پلکانی (`Bulk Discount: 10+, 30+, 100+`)، موجودی (`Available: 322`) و تعداد فروش (`Sold: 2053`) | قابلیت ویرایش کامل متن توضیحات، قوانین، قیمت دلاری/تومانی، تخفیف پلکانی خرید عمده و رنگ دکمه‌های خرید مستقیماً از پنل ادمین. |
| **00:25 تا 00:32** | بخش افزایش موجودی (`💳 Deposit`) | نمایش روش‌های واریز ثابت (`VN Bank QR`, `USDT BEP20`, `LTC BEP20`, `USDC BEP20`, `ByBit Pay`) | **تفکیک هوشمند بر اساس زبان/کشور:** برای تمام زبان‌ها به‌جز فارسی فقط **رمزارزها** (با قابلیت افزودن بی‌نهایت رمزارز، شبکه و آدرس ولت مجزا در پنل ادمین) و برای **زبان فارسی (`fa`)** نمایش هم‌زمان **پرداخت ریالی** (کارت‌به‌کارت با رسید/درگاه بانکی) + رمزارز. |
| **00:37 تا 00:44** | بخش پشتیبانی (`🆘 Support`) | انتخاب نوع تیکت (`💳 Deposit Issue` یا `💬 Other Issue / Live Chat`) و ارسال پیام به ادمین | سیستم تیکتینگ و چت دوطرفه داخل ربات با قابلیت پاسخ مستقیم ادمین، ارسال فایل/عکس و مشاهده تاریخچه پیام‌ها. |
| **00:48 تا 00:53** | بخش موجودی (`💰 Balance`) | نمایش موجودی کاربر به تفکیک ارز و دکمه میانبر `Deposit` | نمایش دقیق موجودی دلاری (`USD`) و ریالی/تومانی (`IRR/IRT`)، تاریخچه تراکنش‌ها و خریدها، و میانبر شارژ حساب. |
| **00:59 تا 01:04** | بخش تغییر زبان (`🌐 Language`) | چیدمان ۲ ستونی شامل ۸ زبان با پرچم کشورها | پوشش **۱۲ زبان پرتکلم دنیا**: ۶ زبان اصلی درخواستی (**فارسی، عربی، انگلیسی، هندی، روسی، آلمانی**) + ۶ زبان پرجمعیت پیشنهادی (**چینی ساده‌شده، اسپانیایی، پرتغالی برزیل، اندونزیایی، فرانسوی، ترکی استانبولی**). |

---

## بخش دوم: پرامپت جامع (Authoritative Master Specification)

# ROLE & OBJECTIVE
You are a Principal Backend & Telegram Bot Architect and Senior Cybersecurity Engineer. Your task is to design, architect, and implement a complete, production-ready, high-security **Multi-Language Telegram Digital Shop Bot & Full-Featured Admin Panel (Telegram In-Bot Admin + Modern Web Admin Dashboard)** inspired by an enterprise digital goods shop bot, customized to the exact functional, UI/UX, payment, and security specifications detailed below.

Do NOT write placeholder code, `TODO` comments, or truncated logic. Deliver a complete, modular, Dockerized, production-grade codebase with database migrations, seed data, and deployment scripts.

---

## 1. TECH STACK & ARCHITECTURE
- **Language & Runtime:** Python 3.11+ (Asyncio)
- **Telegram Bot Framework:** `aiogram 3.x` (strictly async, using FSM with Redis storage, custom middlewares, callback data factories, and inline message editing `edit_text` to prevent chat clutter)
- **Web Admin Panel & Mini App API:** `FastAPI` + `Jinja2` / `React (Next.js or Vite + Tailwind CSS + shadcn/ui)` providing a super user-friendly graphical Admin Panel alongside an in-bot `/admin` panel.
- **Database:** `PostgreSQL 16` with `SQLAlchemy 2.0 (Async)` + `Alembic` for migrations.
- **Cache, FSM & Rate Limiting:** `Redis 7`
- **Containerization:** `Docker` & `docker-compose.yml` (with Nginx reverse proxy and SSL support).

---

## 2. USER-FACING BOT STRUCTURE & NAVIGATION FLOW
All bot navigation MUST work seamlessly via **in-place message editing (`editMessageText` / `editMessageCaption`)** so the chat stays clean like a single-page application. Every sub-menu must include a `🔙 Back` button returning to the exact parent menu.

### 2.1. Strictly Scoped Main Menu (`/start`)
When a user sends `/start`:
1. If first time, prompt language selection (or auto-detect from Telegram `language_code` with instant option to switch).
2. Display the customizable Welcome Banner/Text (editable per language from Admin Panel) showing:
   - User ID & Username
   - Current Wallet Balance (`$ USD` and `تومان / ریال` if Persian language is active)
3. **Main Menu Buttons (ONLY these core sections + Admin-Pinned Featured Products):**
   - **[Optional Row(s)] Pinned Featured Products:** (Configurable by Admin, e.g., `🚀 VERIFY GEMINI STUDENT PRO 12 MONTHS`, with customizable color badge).
   - **Row 1 (Full Width):** `🛒 Shop` (فروشگاه)
   - **Row 2 (2 Columns):** `💳 Deposit` (افزایش موجودی) | `💰 Balance` (موجودی و حساب کاربری)
   - **Row 3 (2 Columns):** `🆘 Support` (پشتیبانی) | `🌐 Language` (تغییر زبان)
   - *(CRITICAL: Do NOT include Referral, Status, Daily Top Invites, Device Verification, or API Docs buttons on the main menu).*

---

## 3. MULTI-LANGUAGE ENGINE (12 LANGUAGES + CMS)
Support **12 high-population languages** out of the box with complete localization dictionaries and right-to-left (RTL) formatting support:
1. **Persian (فارسی) 🇮🇷 (`fa`)** — *Primary for Iran (enables Rial/Toman + Crypto payments)*
2. **English 🇺🇸 (`en`)** — *Global default*
3. **Arabic (العربية) 🇸🇦 (`ar`)**
4. **Hindi (हिन्दी) 🇮🇳 (`hi`)**
5. **Russian (Русский) 🇷🇺 (`ru`)**
6. **German (Deutsch) 🇩🇪 (`de`)**
7. **Chinese Simplified (简体中文) 🇨🇳 (`zh`)** — *Added high-population language*
8. **Spanish (Español) 🇪🇸 (`es`)** — *Added high-population language*
9. **Portuguese (Português) 🇧🇷 (`pt`)** — *Added high-population language*
10. **Indonesian (Bahasa Indonesia) 🇮🇩 (`id`)** — *Added high-population language*
11. **French (Français) 🇫🇷 (`fr`)** — *Added high-population language*
12. **Turkish (Türkçe) 🇹🇷 (`tr`)** — *Added high-population language*

### Dynamic Text & Button Customization (CMS):
- **Every single text string, welcome message, help prompt, and button label** across all 12 languages must be stored in the database (`ui_translations` & `ui_buttons` tables) with Redis caching.
- The Admin must be able to edit the text of ANY section, prompt, or button in ANY language directly from the Admin Panel without touching the code.

---

## 4. DYNAMIC COLOR SYSTEM FOR BUTTONS & PRODUCTS
Implement a dual-layer color customization system controllable from the Admin Panel:
1. **Layer 1 — Native Telegram Inline Keyboard Color Indicators:**
   - Since standard Telegram Bot API inline buttons use system theme backgrounds, allow the Admin to assign a **Color Theme / Status Indicator** to **ANY button and ANY product** from the Admin Panel (`Green 🟢`, `Red 🔴`, `Yellow 🟡`, `Blue 🔵`, `Purple 🟣`, `Orange 🟠`, `Black ⚫`, `White ⚪`, or Custom Emoji/Icon).
   - In the Shop product list, each product button dynamically renders its assigned color indicator (e.g., `🟢 CDK Gemini Pro Xiaomi 3 Month - Stock: 322` or `🔴 Out of Stock / Hot Deal`).
2. **Layer 2 — Telegram Mini App (WebApp) & Web Admin CSS Color Picker:**
   - Provide an optional 1-click Telegram WebApp Shop view and Web Admin Dashboard where every button and product card also has a **Hex/RGB Color Picker** (`#22c55e` green, `#ef4444` red, `#eab308` yellow, `#3b82f6` blue, etc.) that changes the actual CSS button background, border, and badge color in real time.

---

## 5. SHOP, CATEGORIES & PRODUCT INVENTORY SYSTEM (`🛒 Shop`)
1. **Categories View:**
   - Displays active product categories configured by Admin (e.g., `✨ Gemini AI`, `💬 ChatGPT`, `🤖 Claude`, `🎬 CapCut`, etc.) + `📜 Order History` + `🔙 Back`.
2. **Products List View (Inside a Category):**
   - Lists products in that category.
   - Each product button displays: `[Color Indicator] [Product Name] - Stock: [Count]`.
3. **Product Detail View:**
   - Displays localized Product Title, rich Description, Checklist/Rules before purchase, Unit Price (`USD` + `IRR/Toman` for Persian users), **Bulk Discount Tiers** (e.g., `Buy 10+: $1.75 each`, `Buy 30+: $1.70 each`, `Buy 100+: $1.65 each`), Available Stock count, and Total Sold count.
   - **Quantity Selector & Purchase Buttons:** Allow user to select quantity (`1`, `5`, `10`, `Custom Quantity`) and pay via Wallet Balance (`Buy with USD Balance` / `خرید با موجودی تومانی` for `fa` users) or Direct Payment.
4. **Product Delivery Types:**
   - **Auto-Delivery (Stock/Keys/Accounts):** Admin uploads a list of lines/keys/accounts (or text/file items). Upon purchase, the bot atomically locks and delivers the exact quantity of items to the user using `SELECT ... FOR UPDATE SKIP LOCKED` to prevent race conditions (double-selling).
   - **Manual/Service Fulfillment:** For products requiring manual activation or user input (e.g., submitting an email or link), the bot prompts the user for the required input after purchase and sends an instant fulfillment ticket to the Admin.

---

## 6. REGION-AWARE PAYMENT & DEPOSIT SYSTEM (`💳 Deposit`)
### 6.1. Rule-Based Payment Routing by Language/Region:
- **For ALL Languages Except Persian (`en`, `ar`, `hi`, `ru`, `de`, `zh`, `es`, `pt`, `id`, `fr`, `tr`):**
  - Display **ONLY Cryptocurrency Payment Methods**.
- **For Persian Language (`fa` - Iran):**
  - Display **BOTH Iranian Rial/Toman (`IRR/IRT`) Payment Methods AND Cryptocurrency Payment Methods**.

### 6.2. Dynamic Cryptocurrency Management (Admin-Configurable):
- Admin can add, edit, enable/disable, or delete **unlimited cryptocurrencies and networks** in the Admin Panel (e.g., `USDT - TRC20`, `USDT - BEP20`, `USDT - TON`, `BTC`, `ETH`, `LTC`, `TRX`, `SOL`, `TON`, `USDC`, etc.).
- Each crypto method has:
  - Coin Name & Symbol (e.g., `USDT`)
  - Network Name (e.g., `TRC20`, `BEP20`, `ERC20`)
  - **Dedicated Wallet Address** (Admin can set a completely different wallet address for each coin/network)
  - Optional QR Code image upload (or auto-generated QR code from the wallet address)
  - Minimum deposit amount, network fee note, and custom instructions
  - Verification mode:
    1. **Manual/Semi-Auto TXID & Screenshot Verification:** User selects coin -> enters USD amount -> bot displays exact Wallet Address (copyable monospace) + QR Code + instructions -> user submits TXID (Transaction Hash) or receipt screenshot -> Admin receives instant notification with `[✅ Approve & Credit User]` and `[❌ Reject]` buttons.
    2. **Optional Blockchain Explorer / Gateway Webhook Hook:** Modular adapter for TronGrid/BscScan TXID verification or NOWPayments/Oxapay API.

### 6.3. Iranian Rial/Toman Payment System (Exclusive to `fa`):
- Admin can configure:
  1. **Card-to-Card (کارت به کارت):** Admin sets 16-digit Card Number, Sheba (IBAN), Cardholder Name, and Bank Name in Admin Panel. User enters Toman amount -> views card details -> uploads payment receipt photo/text -> forwarded to Admin Payment Queue with 1-click `[✅ تایید و شارژ خودکار حساب]` or `[❌ رد پرداخت]`.
  2. **Iranian Payment Gateway Module (درگاه پرداخت ریالی):** Modular webhook callback handler (ZarinPal / Zibal / NextPay / IDPay compatible) with automatic USD-to-Toman exchange rate conversion (configurable manual rate or live API rate in Admin Panel).

---

## 7. USER-FRIENDLY FULL ADMIN PANEL (WEB DASHBOARD + IN-BOT `/admin`)
Build BOTH an intuitive **In-Bot Admin Panel (`/admin`)** with inline buttons AND a **Modern Web Admin Dashboard (FastAPI + Tailwind UI)** so the admin can manage everything effortlessly from phone or PC:

### 7.1. Dashboard & Analytics
- Total users, active users (24h/7d/30d), total revenue (`USD` & `Toman`), pending deposits count, open support tickets count, and low-stock alerts.

### 7.2. Member Search & Complete User Management
- **Instant Member Search:** Search any user by **Numeric Telegram ID (`user_id`)**, **Username (`@username`)**, or **First/Last Name**.
- **User Profile Inspection & Actions Card:** When viewing a user's panel, Admin can:
  - View full profile: Numeric ID, Username, Selected Language, Registration Date, Current USD/Toman Balance, Total Deposits, Total Orders, and Account Status (`Active` / `Blocked`).
  - **Charge / Deduct Balance (شارژ یا کسر موجودی):** Add or subtract any amount (`USD` or `Toman`) with an optional reason note, automatically sending a notification message to the user.
  - **Block / Unblock User (بلاک و آنبلاک کردن کاربر):** Instantly ban/unban a specific user (blocked users are rejected at the middleware level and cannot use any bot features).
  - **Direct Message to User (ارسال پیام به شخص خاص):** Send a direct text/photo/file message to that specific user through the bot.
  - View the user's complete Deposit History and Order History.

### 7.3. Messaging & Broadcast System
- **Single User Messaging:** Send a message to any specific user by Numeric ID or `@username`.
- **Mass Broadcast (ارسال همگانی به کل ممبرها):** Send text, photo, video, or inline-button announcements to ALL members or filtered segments (e.g., users of a specific language like Persian only, or users with balance > 0), using an async rate-limited queue (25 messages/sec with `FloodWait` retry handling) and live progress bar.

### 7.4. Product, Category & Color Management
- Create, edit, reorder, and delete Categories and Products.
- Add product stock (bulk paste lines/keys/accounts, upload `.txt`/`.csv`, or set unlimited/manual stock).
- Set tiered bulk discounts (`Min Qty -> Discounted Price`).
- **Color Customizer:** Pick the color badge (`🟢 Green`, `🔴 Red`, `🟡 Yellow`, `🔵 Blue`, `🟣 Purple`, `🟠 Orange`, etc.) and Web CSS Hex color for every product and every menu button.

### 7.5. Payment Methods, Wallets & Transactions Queue (`قسمت پرداخت‌ها`)
- **Payment Methods & Wallet Manager:**
  - Add/edit/remove any Cryptocurrency, Network, Wallet Address, QR code, and payment instructions.
  - Add/edit/remove Iranian Rial payment methods (Card-to-Card details, exchange rate, gateway keys).
- **Payments & Deposits Inspector (مدیریت پرداختی‌ها):**
  - Filterable list of all payments (`⏳ Pending`, `✅ Approved`, `❌ Rejected`).
  - View user details, payment method, amount, TXID/Receipt image, timestamp, and approve/reject with 1 click (automatically crediting the user's balance inside an ACID transaction).

### 7.6. Dynamic Text & UI Editor (CMS)
- Edit the text of any section, welcome message, support text, payment instruction, or button label across all 12 languages directly from the panel.

---

## 8. MANDATORY SECURITY & RELIABILITY REQUIREMENTS
You MUST implement all of the following security controls:
1. **Strict Admin Authentication & RBAC:**
   - Middleware-level verification checking `telegram_user_id` against encrypted environment config + database admin table for bot commands, and **JWT + Bcrypt + CSRF protection + Telegram Login Widget / 2FA** for the Web Admin Dashboard.
2. **Atomic Financial & Inventory Transactions (Zero Race Conditions):**
   - Never store balances as floats. Use `NUMERIC(18, 8)` / `Decimal` for all crypto/fiat calculations.
   - Use PostgreSQL row-level locking (`SELECT ... FOR UPDATE`) on `users` balance deduction/charging and `SELECT ... FOR UPDATE SKIP LOCKED` on `product_stock` allocation so concurrent clicks or double-taps can NEVER cause negative balance or double-spend.
   - Maintain an immutable `wallet_ledger` audit table recording every balance change (`before_balance`, `change_amount`, `after_balance`, `reference_type`, `admin_id`, `timestamp`).
3. **Idempotency & Replay Protection:**
   - Enforce `UNIQUE` constraints on crypto `tx_hash` (TXID) and payment gateway `authority/track_id` so the same transaction receipt can never be credited twice.
4. **Anti-Spam, Throttling & Input Sanitization:**
   - Redis-backed sliding-window Rate Limiting Middleware per user (e.g., max 5 requests per 3 seconds, strict cooldown on deposit submissions and support messages).
   - Blocked-user middleware that drops all updates from banned `user_id`s at the outermost layer.
   - Strict HTML/Markdown escaping (`html.escape`) on all user-supplied inputs (names, usernames, support messages, TXIDs) before sending to Admin or rendering in Web Panel to prevent XSS and Telegram parser injection.
   - Validate uploaded receipt images (check MIME type, magic bytes, and max file size <= 5MB).
5. **Secrets & Infrastructure Hardening:**
   - All sensitive tokens (`BOT_TOKEN`, `DB_PASSWORD`, `REDIS_PASSWORD`, `ADMIN_SECRET_KEY`, `ENCRYPTION_KEY`) loaded strictly from `.env` via `pydantic-settings`.
   - Encrypt sensitive delivered product credentials at rest in PostgreSQL using `Fernet (AES-128-CBC + HMAC-SHA256)`.

---

## 9. EXPECTED DELIVERABLES & DIRECTORY STRUCTURE
```text
telegram-shop-bot/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
├── alembic.ini
├── migrations/
├── app/
│   ├── main.py                  # Entrypoint running Bot + FastAPI Admin Panel
│   ├── config.py                # Pydantic settings & security config
│   ├── database/
│   │   ├── session.py           # Async SQLAlchemy engine
│   │   ├── models.py            # Users, Categories, Products, Stock, Payments, CryptoWallets, UI Texts, Ledger
│   │   └── crud.py              # Atomic DB operations with row-level locking
│   ├── locales/
│   │   └── seed_translations.py # Complete 12-language dictionary seed (fa, en, ar, hi, ru, de, zh, es, pt, id, fr, tr)
│   ├── bot/
│   │   ├── middlewares/         # Auth, BanCheck, RateLimit, i18n, Logging
│   │   ├── keyboards/           # Dynamic colored inline keyboards & callback builders
│   │   ├── handlers/
│   │   │   ├── start.py         # Main menu (Shop, Deposit, Balance, Support, Language)
│   │   │   ├── shop.py          # Category -> Colored Product -> Bulk Discount -> Atomic Purchase
│   │   │   ├── deposit.py       # Region-aware Crypto (all) + Rial/Toman (fa only) deposit flow
│   │   │   ├── balance.py       # User wallet & order history
│   │   │   ├── support.py       # Two-way support ticket & live chat
│   │   │   ├── language.py      # 12-language switcher
│   │   │   └── admin_bot.py     # Full in-bot /admin panel (Search, Charge, Ban, Broadcast, Payments, CMS, Colors)
│   └── web_admin/
│       ├── app.py               # FastAPI Web Admin Panel routes & JWT Auth
│       └── templates/           # User-friendly responsive Admin UI (Products, Colors, Wallets, Payments, Users, CMS)
└── README.md                    # Step-by-step setup, security checklist, and Ubuntu/Docker deployment guide
```

## بخش سوم: نکات امنیتی و فنی حیاتی
1. **جلوگیری از باگ Double-Spending (خرید یا شارژ هم‌زمان با دو کلیک سریع):**
   - کسر موجودی و تحویل اکانت با قفل سطح سطر در دیتابیس (`SELECT ... FOR UPDATE SKIP LOCKED`) انجام شود.
2. **چگونگی تغییر رنگ دکمه‌ها و محصولات در تلگرام:**
   - نشانگرهای رنگی (`🔴`, `🟢`, `🟡`, `🔵`, `🟣`, `🟠`, `⚫`, `⚪`) روی دکمه‌های اینلاین + رنگ CSS Hex در Web Admin / Mini App.
3. **ثبت یکتای تراکنش‌ها:**
   - هش تراکنش رمزارز (`TXID`) و کد پیگیری ریالی در دیتابیس دارای قید `UNIQUE` باشند.
4. **استفاده از Webhook به جای Polling در سرور عملیاتی:**
   - پشتیبانی کامل از حالت Webhook پشت Nginx با SSL و هدر امنیتی `X-Telegram-Bot-Api-Secret-Token` (و پشتیبانی حالت Polling برای تست/اجرای بدون دامنه در dev).
