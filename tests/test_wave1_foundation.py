"""Wave 1 Gate Verification tests: schema, constraints, 12-lang seed, security, and spec parity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import struct
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.constants import (
    COLOR_PALETTE,
    DEFAULT_LANGUAGE,
    FIAT_ENABLED_LANGUAGES,
    FORBIDDEN_MAIN_MENU_FEATURES,
    LANGUAGE_ORDER,
    MAIN_MENU_BUTTON_KEYS,
    SUPPORTED_LANGUAGES,
    Currency,
    MenuScope,
    is_valid_hex_color,
    normalize_hex_color,
)
from app.core.exceptions import AuthenticationError, EncryptionError, FileValidationError
from app.core.logging import SecretMaskingFilter
from app.core.security import (
    create_access_token,
    decrypt_secret,
    detect_image_mime,
    encrypt_secret,
    escape_html,
    generate_csrf_token,
    generate_fernet_key,
    hash_password,
    sanitize_username,
    validate_receipt_image,
    validate_stock_upload_file,
    verify_access_token,
    verify_csrf_token,
    verify_password,
    verify_telegram_login_widget,
    verify_telegram_webapp_init_data,
    verify_totp_code,
)
from app.database.models import Payment, User
from app.database.seed import run_seed
from app.locales.i18n import TranslationService
from app.locales.seed_translations import TRANSLATIONS
from app.services.interfaces import (
    AnalyticsServiceProtocol,
    BroadcastServiceProtocol,
    CMSServiceProtocol,
    PaymentServiceProtocol,
    ShopServiceProtocol,
    SupportServiceProtocol,
    UserServiceProtocol,
)

EXPECTED_TABLES: set[str] = {
    "users",
    "wallet_ledger",
    "categories",
    "products",
    "product_bulk_tiers",
    "orders",
    "product_stock",
    "order_items",
    "crypto_wallets",
    "fiat_payment_methods",
    "payments",
    "support_tickets",
    "support_messages",
    "ui_translations",
    "ui_buttons",
    "app_settings",
    "broadcast_jobs",
}

EXPECTED_12_LANGUAGES: tuple[str, ...] = (
    "fa",
    "en",
    "ar",
    "hi",
    "ru",
    "de",
    "zh",
    "es",
    "pt",
    "id",
    "fr",
    "tr",
)


# ---------------------------------------------------------------------------
# 1. PostgreSQL 16 Schema, Precision, Constraints & Immutability Audit
# ---------------------------------------------------------------------------
async def test_all_tables_exist_in_postgres(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
    ).scalars().all()
    table_set = set(rows)
    assert "alembic_version" in table_set
    missing = EXPECTED_TABLES - table_set
    assert not missing, f"Missing expected PostgreSQL tables: {sorted(missing)}"


async def test_numeric_precision_on_financial_columns(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'numeric'"
            )
        )
    ).all()
    precision_map = {
        (row.table_name, row.column_name): (row.numeric_precision, row.numeric_scale)
        for row in rows
    }

    expected_precisions: dict[tuple[str, str], tuple[int, int]] = {
        ("users", "balance_usd"): (18, 8),
        ("users", "balance_irt"): (18, 2),
        ("wallet_ledger", "before_balance"): (18, 8),
        ("wallet_ledger", "change_amount"): (18, 8),
        ("wallet_ledger", "after_balance"): (18, 8),
        ("products", "price_usd"): (18, 8),
        ("products", "price_irt"): (18, 2),
        ("product_bulk_tiers", "unit_price_usd"): (18, 8),
        ("product_bulk_tiers", "unit_price_irt"): (18, 2),
        ("orders", "unit_price"): (18, 8),
        ("orders", "total_amount"): (18, 8),
        ("order_items", "unit_price"): (18, 8),
        ("crypto_wallets", "min_deposit_usd"): (18, 8),
        ("fiat_payment_methods", "min_deposit_irt"): (18, 2),
        ("payments", "amount_usd"): (18, 8),
        ("payments", "amount_irt"): (18, 2),
        ("payments", "exchange_rate_used"): (18, 4),
    }
    for col_key, expected in expected_precisions.items():
        assert precision_map.get(col_key) == expected, (
            f"Column {col_key} precision mismatch: got {precision_map.get(col_key)}, expected {expected}"
        )


async def test_wallet_ledger_immutability_and_fk_restrict(db_session: AsyncSession) -> None:
    cols = (
        await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'wallet_ledger'"
            )
        )
    ).scalars().all()
    col_set = set(cols)
    required_cols = {
        "id",
        "user_id",
        "currency",
        "before_balance",
        "change_amount",
        "after_balance",
        "reference_type",
        "reference_id",
        "admin_id",
        "note",
        "created_at",
    }
    assert required_cols.issubset(col_set)
    assert "updated_at" not in col_set, "wallet_ledger must be append-only without updated_at"

    fk_rows = (
        await db_session.execute(
            text(
                "SELECT conrelid::regclass::text AS table_name, conname, confdeltype::text AS confdeltype "
                "FROM pg_constraint WHERE contype = 'f'"
            )
        )
    ).all()
    fk_deltypes = {(r.table_name, r.conname): r.confdeltype for r in fk_rows}
    restrict_tables = {"wallet_ledger", "orders", "order_items", "payments", "support_tickets"}
    for tbl in restrict_tables:
        matching = [dt for (t, _), dt in fk_deltypes.items() if t == tbl and dt == "r"]
        assert matching, f"Expected ON DELETE RESTRICT foreign key on {tbl}"


async def test_unique_replay_protection_and_check_constraints(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        try:
            idx_rows = (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND tablename = 'payments'"
                    )
                )
            ).all()
            unique_defs = [r.indexdef for r in idx_rows if "UNIQUE INDEX" in r.indexdef.upper()]
            assert any("(tx_hash)" in d for d in unique_defs), "payments.tx_hash must be UNIQUE"
            assert any("(authority_track_id)" in d for d in unique_defs), (
                "payments.authority_track_id must be UNIQUE"
            )

            session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
            user = User(telegram_id=99000111222, username="qa_replay_test", language_code="en")
            session.add(user)
            await session.flush()

            # Null tx_hash / authority_track_id should allow multiple pending records
            p1 = Payment(
                user_id=user.id,
                payment_category="crypto",
                currency="USD",
                amount_usd=Decimal("10.00000000"),
                tx_hash="0xQA_UNIQUE_HASH_001",
            )
            p2 = Payment(
                user_id=user.id,
                payment_category="fiat_gateway",
                currency="IRT",
                amount_irt=Decimal("500000.00"),
                authority_track_id="AUTH-QA-001",
            )
            p3 = Payment(
                user_id=user.id,
                payment_category="fiat_card",
                currency="IRT",
                amount_irt=Decimal("250000.00"),
                tx_hash=None,
                authority_track_id=None,
            )
            session.add_all([p1, p2, p3])
            await session.flush()

            # Duplicate tx_hash must fail
            async with session.begin_nested():
                session.add(
                    Payment(
                        user_id=user.id,
                        payment_category="crypto",
                        currency="USD",
                        amount_usd=Decimal("10.00000000"),
                        tx_hash="0xQA_UNIQUE_HASH_001",
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.flush()

            # Duplicate authority_track_id must fail
            async with session.begin_nested():
                session.add(
                    Payment(
                        user_id=user.id,
                        payment_category="fiat_gateway",
                        currency="IRT",
                        amount_irt=Decimal("500000.00"),
                        authority_track_id="AUTH-QA-001",
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.flush()

            # Negative balance check constraint must fail
            async with session.begin_nested():
                session.add(
                    User(
                        telegram_id=99000111223,
                        balance_usd=Decimal("-0.00000001"),
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.flush()

            await session.close()
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# 2. 12-Language Seed, Strict 5-Button Main Menu & Dual Color System
# ---------------------------------------------------------------------------
async def test_12_language_constants_and_seed_completeness(db_session: AsyncSession) -> None:
    assert tuple(LANGUAGE_ORDER) == EXPECTED_12_LANGUAGES
    assert set(SUPPORTED_LANGUAGES.keys()) == set(EXPECTED_12_LANGUAGES)
    assert DEFAULT_LANGUAGE == "en"
    assert FIAT_ENABLED_LANGUAGES == frozenset({"fa"})
    assert SUPPORTED_LANGUAGES["fa"].is_rtl is True
    assert SUPPORTED_LANGUAGES["ar"].is_rtl is True
    assert all(
        not SUPPORTED_LANGUAGES[code].supports_fiat_irr
        for code in EXPECTED_12_LANGUAGES
        if code != "fa"
    )

    # welcome.balance_irt_line is intentionally populated only for 'fa' (Iranian Toman)
    fa_only_keys = {"welcome.balance_irt_line"}
    placeholder_re = re.compile(r"\b(?:TO" + r"DO|FIX" + r"ME|PLACE" + r"HOLDER)\b", re.IGNORECASE)
    for key, lang_dict in TRANSLATIONS.items():
        for lang in EXPECTED_12_LANGUAGES:
            assert lang in lang_dict, f"Missing language key {lang} in {key}"
            val = lang_dict[lang]
            assert isinstance(val, str)
            if key in fa_only_keys and lang != "fa":
                continue
            assert val.strip(), f"Empty translation in seed for {key}.{lang}"
            assert not placeholder_re.search(val), f"Placeholder marker found in {key}.{lang}: {val!r}"

    db_rows = (
        await db_session.execute(
            text("SELECT key, lang_code, text_value FROM ui_translations")
        )
    ).all()
    db_map = {(r.key, r.lang_code): r.text_value for r in db_rows}
    assert len(db_map) >= len(TRANSLATIONS) * len(EXPECTED_12_LANGUAGES)
    for key in TRANSLATIONS:
        for lang in EXPECTED_12_LANGUAGES:
            assert (key, lang) in db_map, f"Missing DB translation row for ({key}, {lang})"
            if key in fa_only_keys and lang != "fa":
                continue
            assert db_map[(key, lang)].strip(), f"Blank DB translation row for ({key}, {lang})"


async def test_strict_5_button_main_menu_and_dual_colors(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client,
) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT button_key, menu_scope, row_index, col_index, color_emoji, color_hex, is_active, labels_json "
                "FROM ui_buttons WHERE menu_scope = 'main' ORDER BY row_index, col_index"
            )
        )
    ).all()

    keys = [r.button_key for r in rows]
    assert len(rows) == 5, f"Main menu must have strictly 5 buttons, got {keys}"
    assert set(keys) == set(MAIN_MENU_BUTTON_KEYS)
    for forbidden in FORBIDDEN_MAIN_MENU_FEATURES:
        assert forbidden not in keys

    for r in rows:
        assert r.is_active is True
        assert r.color_emoji.strip()
        assert is_valid_hex_color(r.color_hex), f"Invalid hex color on {r.button_key}: {r.color_hex}"
        for lang in EXPECTED_12_LANGUAGES:
            assert r.labels_json.get(lang, "").strip(), f"Missing label for {r.button_key}.{lang}"

    # Verify Redis-cached TranslationService lookup and invalidation
    i18n = TranslationService(db_session_factory, redis_client=redis_client, cache_ttl=120)
    await i18n.invalidate_cache()

    buttons = await i18n.get_main_menu_buttons("fa")
    assert [b.button_key for b in buttons] == ["shop", "deposit", "balance", "support", "language"]
    cached_buttons = await redis_client.get(f"{TranslationService.BUTTONS_CACHE_PREFIX}{MenuScope.MAIN.value}")
    assert cached_buttons is not None

    fa_text = await i18n.get_text(
        "welcome.banner",
        "fa",
        user_id=12345,
        username="@testuser",
        balance_usd="10.00",
        balance_irt_line="",
    )
    assert "12345" in fa_text
    assert "@testuser" in fa_text
    assert await redis_client.get(f"{TranslationService.TEXTS_CACHE_PREFIX}fa") is not None

    # Temporary CMS write + automatic cache invalidation + cleanup
    tmp_key = "qa.temp_wave1_probe"
    try:
        await i18n.set_translation(tmp_key, "en", "Initial Value")
        assert await i18n.get_text(tmp_key, "en") == "Initial Value"
        await i18n.set_translation(tmp_key, "en", "Updated Value")
        assert await i18n.get_text(tmp_key, "en") == "Updated Value"
    finally:
        await db_session.execute(text("DELETE FROM ui_translations WHERE key = :k"), {"k": tmp_key})
        await db_session.commit()
        await i18n.invalidate_cache("en")


def test_dual_color_palette_and_hex_helpers() -> None:
    expected_emojis = {"🟢", "🔴", "🟡", "🔵", "🟣", "🟠", "⚫", "⚪"}
    actual_emojis = {preset.emoji for preset in COLOR_PALETTE.values()}
    assert expected_emojis.issubset(actual_emojis)
    for preset in COLOR_PALETTE.values():
        assert is_valid_hex_color(preset.hex_code)

    assert normalize_hex_color("#22C55E") == "#22c55e"
    assert normalize_hex_color(None) == "#3b82f6"
    assert is_valid_hex_color("#ef4444") is True
    assert is_valid_hex_color("red") is False
    with pytest.raises(ValueError):
        normalize_hex_color("#GGGGGG")


async def test_app_settings_and_seed_idempotency(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    keys = set(
        (await db_session.execute(text("SELECT key FROM app_settings"))).scalars().all()
    )
    assert {
        "usd_to_toman_rate",
        "rate_mode",
        "maintenance_mode",
        "pinned_limit",
        "low_stock_default_threshold",
    }.issubset(keys)

    async with db_session_factory() as fresh_session:
        second_run = await run_seed(fresh_session)
    assert second_run == {
        "translations_added": 0,
        "buttons_added": 0,
        "settings_added": 0,
        "products_added": 0,
    }


# ---------------------------------------------------------------------------
# 3. Mandatory Security Controls Verification
# ---------------------------------------------------------------------------
def test_fernet_encryption_roundtrip_and_failure_cases() -> None:
    key1 = generate_fernet_key()
    key2 = generate_fernet_key()
    secret_payload = "user@example.com:Sup3rS3cret!|پکیج-تست"

    ciphertext = encrypt_secret(secret_payload, key1)
    assert ciphertext != secret_payload
    assert ciphertext.startswith("gAAAAA")
    assert decrypt_secret(ciphertext, key1) == secret_payload

    with pytest.raises(EncryptionError):
        decrypt_secret(ciphertext, key2)
    with pytest.raises(EncryptionError):
        decrypt_secret("not-a-valid-token", key1)
    with pytest.raises(EncryptionError):
        encrypt_secret("abc", "invalid-key")


def test_bcrypt_and_jwt_helpers() -> None:
    raw_pw = secrets.token_urlsafe(16)
    pw_hash = hash_password(raw_pw)
    assert pw_hash.startswith("$2")
    assert verify_password(raw_pw, pw_hash) is True
    assert verify_password("wrong-password-123", pw_hash) is False

    with pytest.raises(ValueError):
        hash_password("short")

    jwt_secret = secrets.token_hex(32)
    token = create_access_token("admin", jwt_secret, expires_minutes=5, extra_claims={"role": "superadmin"})
    claims = verify_access_token(token, jwt_secret)
    assert claims["sub"] == "admin"
    assert claims["role"] == "superadmin"

    with pytest.raises(AuthenticationError):
        verify_access_token(token, secrets.token_hex(32))

    expired = create_access_token("admin", jwt_secret, expires_minutes=-1)
    with pytest.raises(AuthenticationError):
        verify_access_token(expired, jwt_secret)


def test_csrf_telegram_widget_webapp_and_totp() -> None:
    csrf_secret = secrets.token_hex(32)
    csrf_tok = generate_csrf_token(csrf_secret, "sess-123")
    assert verify_csrf_token(csrf_tok, csrf_secret, "sess-123") is True
    assert verify_csrf_token(csrf_tok, csrf_secret, "sess-999") is False
    assert verify_csrf_token(csrf_tok[:-2] + "00", csrf_secret, "sess-123") is False

    ephemeral_bot_token = f"999999999:{secrets.token_urlsafe(24)}"
    now = int(time.time())

    # Telegram Login Widget HMAC check
    login_fields = {"id": "123456", "first_name": "Ali", "username": "ali_test", "auth_date": str(now)}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(login_fields.items()))
    login_secret = hashlib.sha256(ephemeral_bot_token.encode("utf-8")).digest()
    login_hash = hmac.new(login_secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    assert verify_telegram_login_widget({**login_fields, "hash": login_hash}, ephemeral_bot_token) is True
    assert verify_telegram_login_widget({**login_fields, "first_name": "Evil", "hash": login_hash}, ephemeral_bot_token) is False

    # Telegram WebApp initData HMAC check
    webapp_pairs = {"auth_date": str(now), "query_id": "AAHdF6IQ", "user": '{"id":123456,"first_name":"Ali"}'}
    wa_check = "\n".join(f"{k}={v}" for k, v in sorted(webapp_pairs.items()))
    wa_secret = hmac.new(b"WebAppData", ephemeral_bot_token.encode("utf-8"), hashlib.sha256).digest()
    wa_hash = hmac.new(wa_secret, wa_check.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = "&".join(f"{k}={v}" for k, v in {**webapp_pairs, "hash": wa_hash}.items())
    parsed = verify_telegram_webapp_init_data(init_data, ephemeral_bot_token)
    assert parsed["user"]["id"] == 123456
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data.replace("Ali", "Mallory"), ephemeral_bot_token)

    # TOTP RFC 6238 check
    raw_totp = secrets.token_bytes(20)
    secret_b32 = base64.b32encode(raw_totp).decode("ascii").rstrip("=")
    counter = int(time.time()) // 30
    digest = hmac.new(raw_totp, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    assert verify_totp_code(secret_b32, f"{code_int:06d}") is True
    assert verify_totp_code(secret_b32, "000000" if code_int != 0 else "999999") is False


def test_html_escaping_and_upload_validation() -> None:
    xss = '<script>alert("xss")</script> & <b>bold</b>'
    escaped = escape_html(xss)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&quot;" in escaped
    assert sanitize_username("@evil<tag>") == "@evil&lt;tag&gt;"
    assert sanitize_username(None) == "-"

    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32

    assert detect_image_mime(jpeg_bytes) == "image/jpeg"
    assert detect_image_mime(png_bytes) == "image/png"
    assert detect_image_mime(webp_bytes) == "image/webp"

    assert validate_receipt_image(jpeg_bytes, filename="r.jpg", content_type="image/jpeg") == "image/jpeg"
    assert validate_receipt_image(png_bytes, filename="r.png", content_type="image/png") == "image/png"
    assert validate_receipt_image(webp_bytes, filename="r.webp", content_type="image/webp") == "image/webp"

    with pytest.raises(FileValidationError):
        validate_receipt_image(b"<html>not an image</html>", filename="evil.jpg", content_type="image/jpeg")
    with pytest.raises(FileValidationError):
        validate_receipt_image(jpeg_bytes, filename="r.png", content_type="image/jpeg")
    with pytest.raises(FileValidationError):
        validate_receipt_image(b"\xff\xd8\xff\xe0" + b"\x00" * (5 * 1024 * 1024))

    stock_lines = validate_stock_upload_file(b"acc1:pw1\n\nacc2:pw2\r\n", filename="keys.txt")
    assert stock_lines == ["acc1:pw1", "acc2:pw2"]
    with pytest.raises(FileValidationError):
        validate_stock_upload_file(b"\xff\xd8\xff\x00binary", filename="keys.txt")
    with pytest.raises(FileValidationError):
        validate_stock_upload_file(b"acc1:pw1", filename="keys.exe")


def test_secret_masking_log_filter() -> None:
    flt = SecretMaskingFilter()
    sample_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"
    sample_fernet = "gAAAAABn1234567890abcdefghijklmnop=="
    rec = logging.LogRecord(
        name="qa",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=%s bearer=%s db=%s fernet=%s",
        args=(
            sample_token,
            "Bearer abc.def.ghi",
            "postgresql+asyncpg://shopbot:secretpass@127.0.0.1:5432/db",
            sample_fernet,
        ),
        exc_info=None,
    )
    assert flt.filter(rec) is True
    rendered = rec.getMessage()
    assert sample_token not in rendered
    assert "abc.def.ghi" not in rendered
    assert "secretpass" not in rendered
    assert sample_fernet not in rendered


# ---------------------------------------------------------------------------
# 4. Git Hygiene, Hardcoded Secret Scan & Spec Parity Verification
# ---------------------------------------------------------------------------
def test_env_gitignored_and_zero_hardcoded_secrets(project_root: Path) -> None:
    check_ignore = subprocess.run(
        ["git", "check-ignore", "-q", ".env"],
        cwd=project_root,
        check=False,
    )
    assert check_ignore.returncode == 0, ".env MUST be ignored by .gitignore"

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert ".env" not in tracked
    assert ".env.example" in tracked or (project_root / ".env.example").exists()

    # Scan source code under app/, migrations/, tests/ for hardcoded secret literals or stubs
    secret_patterns = [
        re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}\b"),
        re.compile(r"(?:postgresql(?:\+asyncpg)?|redis)://(?!\{)[^:\s'\"{}]+:(?!\{)[^@\s'\"{}]+@(?!127\.0\.0\.1|localhost|\{)"),
    ]
    stub_tokens = ("TO" + "DO", "FIX" + "ME", "NotImplemented" + "Error")

    for folder in ("app", "migrations", "tests"):
        for py_file in (project_root / folder).rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for pat in secret_patterns:
                assert not pat.search(content), f"Potential hardcoded secret in {py_file}"
            if py_file.name != "test_wave1_foundation.py":
                for stub in stub_tokens:
                    assert stub not in content, f"Forbidden stub '{stub}' in {py_file}"


def test_architecture_contracts_and_service_interfaces_parity(
    project_root: Path,
    app_settings: Settings,
) -> None:
    for doc_name in (
        "MASTER_PROMPT.md",
        "REQUIREMENTS_CHECKLIST.md",
        "ARCHITECTURE_CONTRACTS.md",
        "FILE_OWNERSHIP.md",
        "CODING_STANDARDS.md",
    ):
        doc_path = project_root / "docs" / doc_name
        assert doc_path.exists() and doc_path.stat().st_size > 500, f"Missing or empty doc: {doc_name}"

    arch_text = (project_root / "docs" / "ARCHITECTURE_CONTRACTS.md").read_text(encoding="utf-8")
    for expected_contract in (
        "MenuCB",
        "LangCB",
        "CatCB",
        "ProdCB",
        "QtyCB",
        "OrderCB",
        "DepCB",
        "PayCB",
        "SupCB",
        "AdmCB",
        "ShopStates",
        "DepositStates",
        "SupportStates",
        "AdminStates",
        "BanCheckMiddleware",
        "RateLimitMiddleware",
    ):
        assert expected_contract in arch_text, f"Missing contract {expected_contract} in ARCHITECTURE_CONTRACTS.md"

    required_methods = {
        UserServiceProtocol: {
            "upsert_telegram_user",
            "get_by_id",
            "get_by_telegram_id",
            "search_users",
            "list_users",
            "set_language",
            "set_blocked",
            "adjust_balance",
            "get_wallet_history",
        },
        ShopServiceProtocol: {
            "list_categories",
            "create_category",
            "update_category",
            "delete_category",
            "list_products",
            "get_product_detail",
            "create_product",
            "update_product",
            "delete_product",
            "set_bulk_tiers",
            "add_auto_stock",
            "calculate_unit_price",
            "purchase_product",
            "submit_manual_fulfillment_input",
            "fulfill_manual_order",
            "cancel_and_refund_order",
            "get_order_detail",
            "list_user_orders",
            "list_orders",
        },
        PaymentServiceProtocol: {
            "get_available_methods_for_language",
            "list_crypto_wallets",
            "create_crypto_wallet",
            "list_fiat_methods",
            "create_fiat_method",
            "create_crypto_deposit",
            "create_fiat_deposit",
            "review_payment",
            "verify_gateway_callback",
            "get_exchange_rate",
            "update_exchange_rate",
        },
        SupportServiceProtocol: {
            "create_ticket",
            "add_message",
            "get_ticket_detail",
            "list_user_tickets",
            "list_tickets",
            "close_ticket",
        },
        CMSServiceProtocol: {
            "get_translation",
            "list_translations",
            "upsert_translation",
            "list_buttons",
            "update_button",
            "get_setting",
            "set_setting",
        },
        BroadcastServiceProtocol: {
            "create_job",
            "resolve_target_telegram_ids",
            "update_progress",
            "get_job",
            "list_jobs",
        },
        AnalyticsServiceProtocol: {"get_dashboard_stats"},
    }
    for proto, methods in required_methods.items():
        for method_name in methods:
            fn = getattr(proto, method_name, None)
            assert fn is not None, f"{proto.__name__} missing {method_name}"
            assert inspect.iscoroutinefunction(fn), f"{proto.__name__}.{method_name} must be async"

    assert app_settings.USD_TO_TOMAN_RATE > Decimal("0")
    assert Currency.USD.value == "USD"
