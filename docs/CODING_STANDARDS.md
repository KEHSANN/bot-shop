# Coding Standards & Security Rules

All Wave 1 and Wave 2 engineers MUST adhere strictly to every standard below.

---

## 1. Asynchronous I/O Only
- Use `async def` for all database, Redis, HTTP (`httpx.AsyncClient`), and Telegram Bot API calls.
- NEVER use `requests`, `urllib`, `time.sleep`, or synchronous SQLAlchemy `Session` in application code.
- Offload CPU-bound crypto/image work (e.g., bcrypt hashing, QR code generation) via `asyncio.to_thread(...)` when invoked from async request/update paths.

## 2. Financial Precision (Zero Floats)
- **NEVER** use `float` for monetary amounts, prices, balances, exchange rates, or bulk discount calculations.
- Always use `decimal.Decimal` in Python and `NUMERIC(18, 8)` (USD/crypto/ledger) or `NUMERIC(18, 2)` (IRT/Toman) or `NUMERIC(18, 4)` (exchange rate) in PostgreSQL.
- Quantize USD values with `ROUND_HALF_UP` to `Decimal("0.00000001")` (or `Decimal("0.01")` for display) and IRT values to `Decimal("0.01")` / integer Toman as appropriate.
- Validate all incoming amounts are strictly positive (`> Decimal("0")`) and finite (`amount.is_finite()`).

## 3. Concurrency, Locking & Auditability
- **Wallet Mutations:** Every deposit credit, purchase debit, refund, or admin balance adjustment MUST lock the target `users` row first with `SELECT ... FROM users WHERE id = :id FOR UPDATE` inside an ACID transaction (`async with session.begin():` or active transaction).
- **Stock Allocation:** Auto-delivery stock allocation MUST select rows with:
  ```python
  stmt = (
      select(ProductStock)
      .where(ProductStock.product_id == product_id, ProductStock.status == StockStatus.AVAILABLE.value)
      .order_by(ProductStock.id.asc())
      .limit(quantity)
      .with_for_update(skip_locked=True)
  )
  ```
  If `len(locked_rows) < quantity`, rollback immediately and raise `InsufficientStockError`.
- **Immutable Ledger:** Every change to `users.balance_usd` or `users.balance_irt` MUST append a corresponding `WalletLedger` record in the SAME database transaction. NEVER execute `UPDATE` or `DELETE` on `wallet_ledger`.
- **Idempotency:** Catch PostgreSQL `UniqueViolationError` (`IntegrityError`) when inserting `payments.tx_hash` or `payments.authority_track_id` and raise `DuplicateTransactionError`. Normalize empty strings to `None`.

## 4. Security & Input Sanitization
- **HTML Escaping:** Telegram messages using `ParseMode.HTML` and Jinja2/HTML views MUST escape every user-supplied or untrusted string (`first_name`, `last_name`, `username`, `tx_hash`, `support_message`, `fulfillment_input`, `admin_note`) using `escape_html(...)` from `app.core.security`.
- **Encryption at Rest:** Encrypt all `ProductStock.encrypted_payload`, `OrderItem.encrypted_delivered_payload`, and `FiatPaymentMethod.gateway_merchant_id_encrypted` values using `encrypt_secret(...)` before persisting to PostgreSQL. Decrypt only at delivery/gateway invocation time via `decrypt_secret(...)`.
- **File Uploads:** Always pass uploaded receipt bytes through `validate_receipt_image(...)` (`<= 5 MB`, magic-byte check for JPEG/PNG/WEBP) before saving or forwarding.
- **No Hardcoded Secrets:** Never place tokens, keys, passwords, or fallback secret literals in source files or tests (tests must generate ephemeral test keys via `generate_fernet_key()` / `secrets.token_hex()`).
- **Secrets in Logs:** Never log `BOT_TOKEN`, `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `DB_PASSWORD`, decrypted stock payloads, or full card/merchant credentials.

## 5. Error Handling & Code Completeness
- **No Silent Failures:** Never write bare `except: pass` or `except Exception: pass`. Log errors with context (`logger.exception(...)`) or raise explicit domain exceptions from `app.core.exceptions`.
- **No Placeholders or Truncations:** Zero `TODO`, `FIXME`, `NotImplementedError` stubs in deliverable modules, or truncated `# ...` blocks.
- **Clean Type Hints & Docstrings:** Use Python 3.11+ compatible typing (`from __future__ import annotations`, `X | Y`, `list[...]`, `dict[...]`), Pydantic v2 `BaseModel`, and concise docstrings on all classes and public functions.
