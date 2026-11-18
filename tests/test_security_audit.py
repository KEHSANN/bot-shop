"""Wave 3 Security Audit & Static Hardening Verification tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from httpx import ASGITransport
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.dispatcher import create_full_dispatcher
from app.bot.keyboards.common_kb import AdmCB, MenuCB, PayCB
from app.config import Settings
from app.core.constants import DeliveryType
from app.core.exceptions import FileValidationError
from app.core.security import (
    escape_html,
    generate_fernet_key,
    hash_password,
    validate_receipt_image,
)
from app.database.models import Category, CryptoWallet, Payment, Product, User, WalletLedger
from app.locales.i18n import TranslationService
from app.main import create_app
from app.services.dtos import (
    CategoryCreateDTO,
    CryptoDepositCreateDTO,
    CryptoWalletCreateDTO,
    ProductCreateDTO,
    UserCreateOrUpdateDTO,
)
from app.services.payment_service import PaymentService
from app.services.shop_service import ShopService
from app.services.user_service import UserService
from tests.test_integration_e2e import RecordingBot, _cb_update, _msg_update


def _unique_tg_id() -> int:
    return 8_000_000_000 + secrets.randbelow(800_000_000)


def _sign_telegram_login_widget(fields: dict[str, str], bot_token: str) -> dict[str, str]:
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    sig = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**fields, "hash": sig}


def _sign_webapp_init_data(user_obj: dict[str, Any], bot_token: str, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_obj, separators=(",", ":")),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": sig})


def _compute_totp(raw_secret: bytes) -> str:
    counter = int(time.time()) // 30
    digest = hmac.new(raw_secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code_int:06d}"


async def test_security_admin_rbac_ban_drop_and_redis_rate_limit(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    attacker_id = _unique_tg_id()
    banned_id = _unique_tg_id()
    spammer_id = _unique_tg_id()
    raw_totp = secrets.token_bytes(20)
    sec_settings = app_settings.model_copy(
        update={
            "APP_ENV": "test",
            "ADMIN_TELEGRAM_IDS": [admin_id],
            "RATE_LIMIT_MAX_REQUESTS": 3,
            "RATE_LIMIT_WINDOW_SECONDS": 5,
            "TOTP_SECRET": base64.b32encode(raw_totp).decode("ascii").rstrip("="),
        }
    )

    user_service = UserService(db_session_factory, settings=sec_settings)
    payment_service = PaymentService(session_factory=db_session_factory, settings=sec_settings)
    wallet = await payment_service.create_crypto_wallet(
        CryptoWalletCreateDTO(
            coin_name="SecUSDT",
            symbol="USDT",
            network="TRC20",
            wallet_address="TSecWallet1234567890",
            min_deposit_usd=Decimal("1.00"),
        )
    )
    attacker = await user_service.upsert_telegram_user(
        UserCreateOrUpdateDTO(telegram_id=attacker_id, username="attacker", language_code="en")
    )
    pending_pay = await payment_service.create_crypto_deposit(
        CryptoDepositCreateDTO(
            user_id=attacker.id,
            crypto_wallet_id=wallet.id,
            amount_usd=Decimal("50.00"),
            tx_hash=f"0xSEC_RBAC_{secrets.token_hex(8)}",
        )
    )
    banned_dto = await user_service.upsert_telegram_user(
        UserCreateOrUpdateDTO(telegram_id=banned_id, username="banned_user", language_code="en")
    )
    await user_service.set_blocked(banned_dto.id, blocked=True, admin_id=admin_id)

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=sec_settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        user_service=user_service,
        payment_service=payment_service,
    )

    try:
        await dp.feed_update(bot, _msg_update(1, tg_id=attacker_id, text="/admin"))
        assert any("Access denied" in t for t in bot.texts_for(attacker_id))

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(2, tg_id=attacker_id, data=AdmCB(sec="usr", act="list").pack()))
        alert = bot.last_answer_alert()
        assert alert is not None and "Access denied" in (alert.text or "")

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(3, tg_id=attacker_id, data=PayCB(id=pending_pay.id, action="approve").pack()))
        alert2 = bot.last_answer_alert()
        assert alert2 is not None and "Access denied" in (alert2.text or "")
        assert (await payment_service.get_payment(pending_pay.id)).status.value == "pending"

        bot.clear_calls()
        await dp.feed_update(bot, _msg_update(4, tg_id=banned_id, text="/start"))
        await dp.feed_update(bot, _cb_update(5, tg_id=banned_id, data=MenuCB(action="shop").pack()))
        assert bot.calls == [], f"Blocked user should receive zero replies, got {bot.calls}"

        bot.clear_calls()
        for seq in range(1, 6):
            await dp.feed_update(
                bot,
                _cb_update(100 + seq, tg_id=spammer_id, data=MenuCB(action="balance").pack()),
            )
        alerts = [c for c in bot.calls if hasattr(c, "show_alert") and getattr(c, "show_alert", False)]
        assert len(alerts) >= 2
    finally:
        await dp.storage.close()
        async with db_session_factory() as session:
            async with session.begin():
                uids = list(
                    (
                        await session.execute(
                            select(User.id).where(User.telegram_id.in_([attacker_id, banned_id, spammer_id]))
                        )
                    ).scalars().all()
                )
                if uids:
                    await session.execute(delete(WalletLedger).where(WalletLedger.user_id.in_(uids)))
                    await session.execute(delete(Payment).where(Payment.user_id.in_(uids)))
                    await session.execute(delete(User).where(User.id.in_(uids)))
                await session.execute(delete(CryptoWallet).where(CryptoWallet.id == wallet.id))
        for uid in (attacker_id, banned_id, spammer_id):
            await redis_client.delete(f"rl:win:{uid}", f"rl:alert:{uid}")


async def test_security_web_auth_csrf_totp_miniapp_xss_and_webhook(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    tmp_path: Path,
) -> None:
    admin_tg = _unique_tg_id()
    non_admin_tg = _unique_tg_id()
    blocked_tg = _unique_tg_id()
    xss_tg = _unique_tg_id()

    plain_pw = "AuditAdmin!" + secrets.token_hex(6)
    raw_totp = secrets.token_bytes(20)
    totp_b32 = base64.b32encode(raw_totp).decode("ascii").rstrip("=")
    bot_token = f"{200000 + secrets.randbelow(700000)}:AA{secrets.token_urlsafe(28)}"
    webhook_secret = secrets.token_hex(24)

    sec_settings = app_settings.model_copy(
        update={
            "APP_ENV": "test",
            "BOT_TOKEN": bot_token,
            "BOT_MODE": "webhook",
            "BOT_WEBHOOK_PATH": "/webhook/telegram",
            "BOT_WEBHOOK_SECRET": webhook_secret,
            "ADMIN_TELEGRAM_IDS": [admin_tg],
            "WEB_ADMIN_USERNAME": "audit_admin",
            "WEB_ADMIN_PASSWORD_HASH": hash_password(plain_pw),
            "JWT_SECRET_KEY": secrets.token_hex(32),
            "CSRF_SECRET_KEY": secrets.token_hex(32),
            "ENCRYPTION_KEY": generate_fernet_key(),
            "TWO_FACTOR_ENABLED": True,
            "TOTP_SECRET": totp_b32,
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )

    user_service = UserService(db_session_factory, settings=sec_settings)
    shop_service = ShopService(session_factory=db_session_factory, encryption_key=sec_settings.ENCRYPTION_KEY)

    cat = await shop_service.create_category(
        CategoryCreateDTO(
            slug=f"sec-cat-{secrets.token_hex(4)}",
            color_emoji="🔴",
            color_hex="#ef4444",
            names_json={"en": "Sec Cat"},
        )
    )
    prod = await shop_service.create_product(
        ProductCreateDTO(
            category_id=cat.id,
            slug=f"sec-prod-{secrets.token_hex(4)}",
            delivery_type=DeliveryType.MANUAL,
            manual_stock_count=-1,
            price_usd=Decimal("5.00"),
            price_irt=Decimal("300000"),
            titles_json={"en": "Sec Item"},
        )
    )

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=sec_settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        user_service=user_service,
        shop_service=shop_service,
    )

    app = create_app(settings=sec_settings)
    app.state.session_factory = db_session_factory
    app.state.redis = redis_client
    app.state.translation_service = TranslationService(db_session_factory, redis_client=redis_client)
    app.state.i18n = app.state.translation_service
    app.state.user_service = user_service
    app.state.shop_service = shop_service
    app.state.bot = bot
    app.state.dp = dp

    try:
        xss_user = await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=xss_tg,
                username="xss_probe",
                first_name='<script>alert("xss")</script>',
                language_code="en",
            )
        )
        blocked_user = await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=blocked_tg, username="blocked_ma", language_code="en")
        )
        await user_service.set_blocked(blocked_user.id, blocked=True, admin_id=admin_tg)

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/admin/api/dashboard")).status_code == 401

            r_no_csrf = await client.post(
                "/admin/api/auth/login",
                json={"username": "audit_admin", "password": plain_pw, "totp_code": _compute_totp(raw_totp)},
            )
            assert r_no_csrf.status_code == 403

            csrf_res = await client.get("/admin/auth/csrf")
            assert csrf_res.status_code == 200
            csrf_tok = csrf_res.json()["csrf_token"]

            r_bad_pw = await client.post(
                "/admin/api/auth/login",
                headers={"X-CSRF-Token": csrf_tok},
                json={"username": "audit_admin", "password": "wrong-password", "totp_code": _compute_totp(raw_totp)},
            )
            assert r_bad_pw.status_code == 401

            bad_totp = "000000" if _compute_totp(raw_totp) != "000000" else "999999"
            r_bad_totp = await client.post(
                "/admin/api/auth/login",
                headers={"X-CSRF-Token": csrf_tok},
                json={"username": "audit_admin", "password": plain_pw, "totp_code": bad_totp},
            )
            assert r_bad_totp.status_code == 401

            r_ok = await client.post(
                "/admin/api/auth/login",
                headers={"X-CSRF-Token": csrf_tok},
                json={"username": "audit_admin", "password": plain_pw, "totp_code": _compute_totp(raw_totp)},
            )
            assert r_ok.status_code == 200
            body_ok = r_ok.json()
            assert body_ok["ok"] is True
            session_csrf = body_ok["csrf_token"]
            set_cookie_header = " ".join(r_ok.headers.get_list("set-cookie")).lower()
            assert "httponly" in set_cookie_header and "samesite=lax" in set_cookie_header

            r_csrf_block = await client.post(
                "/admin/cms/settings",
                data={"pinned_limit": "4", "maintenance_mode": "false", "low_stock_default_threshold": "5"},
            )
            assert r_csrf_block.status_code == 403

            r_bad_img = await client.post(
                f"/admin/users/{xss_user.id}/message",
                headers={"X-CSRF-Token": session_csrf, "Accept": "application/json"},
                data={"message_text": "hello"},
                files={"photo": ("evil.jpg", b"<html>not-a-jpeg</html>", "image/jpeg")},
            )
            assert r_bad_img.status_code == 400

            r_users_html = await client.get("/admin/users", headers={"Accept": "text/html"})
            assert r_users_html.status_code == 200
            assert '<script>alert("xss")</script>' not in r_users_html.text
            assert "&lt;script&gt;alert" in r_users_html.text

            now_str = str(int(time.time()))
            tampered_widget = _sign_telegram_login_widget(
                {"id": str(admin_tg), "first_name": "Adm", "auth_date": now_str},
                bot_token,
            )
            tampered_widget["first_name"] = "Mallory"
            assert (
                await client.get(
                    "/admin/auth/telegram",
                    params=tampered_widget,
                    headers={"Accept": "application/json"},
                )
            ).status_code == 401

            non_admin_widget = _sign_telegram_login_widget(
                {"id": str(non_admin_tg), "first_name": "Guest", "auth_date": now_str},
                bot_token,
            )
            assert (
                await client.get(
                    "/admin/auth/telegram",
                    params=non_admin_widget,
                    headers={"Accept": "application/json"},
                )
            ).status_code == 403

            admin_widget = _sign_telegram_login_widget(
                {"id": str(admin_tg), "first_name": "Boss", "auth_date": now_str},
                bot_token,
            )
            r_w_ok = await client.get(
                "/admin/auth/telegram",
                params=admin_widget,
                headers={"Accept": "application/json"},
            )
            assert r_w_ok.status_code == 200 and r_w_ok.json()["ok"] is True

            assert (await client.get("/miniapp/api/bootstrap")).status_code == 401
            valid_init = _sign_webapp_init_data(
                {"id": non_admin_tg, "first_name": "Mini", "language_code": "en"},
                bot_token,
            )
            assert (
                await client.get(
                    "/miniapp/api/bootstrap",
                    headers={"X-Telegram-Init-Data": valid_init + "tamper"},
                )
            ).status_code == 401

            blocked_init = _sign_webapp_init_data(
                {"id": blocked_tg, "first_name": "Blk", "language_code": "en"},
                bot_token,
            )
            assert (
                await client.get(
                    "/miniapp/api/bootstrap",
                    headers={"X-Telegram-Init-Data": blocked_init},
                )
            ).status_code == 403

            assert (
                await client.post(
                    "/miniapp/api/purchase",
                    headers={"X-Telegram-Init-Data": valid_init},
                    json={"product_id": prod.id, "quantity": 1, "currency": "IRT"},
                )
            ).status_code == 400

            valid_update_payload = {
                "update_id": 9991,
                "message": {
                    "message_id": 1,
                    "date": int(time.time()),
                    "chat": {"id": non_admin_tg, "type": "private"},
                    "from": {"id": non_admin_tg, "is_bot": False, "first_name": "WH", "language_code": "en"},
                    "text": "/start",
                },
            }
            assert (await client.post("/webhook/telegram", json=valid_update_payload)).status_code == 403
            assert (
                await client.post(
                    "/webhook/telegram",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                    json=valid_update_payload,
                )
            ).status_code == 403
            r_wh_ok = await client.post(
                "/webhook/telegram",
                headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
                json=valid_update_payload,
            )
            assert r_wh_ok.status_code == 200 and r_wh_ok.json()["ok"] is True

        oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (5 * 1024 * 1024 + 1)
        with pytest.raises(FileValidationError):
            validate_receipt_image(oversized, filename="big.jpg", content_type="image/jpeg")
        assert "&lt;script&gt;" in escape_html("<script>")
    finally:
        await dp.storage.close()
        async with db_session_factory() as session:
            async with session.begin():
                uids = list(
                    (
                        await session.execute(
                            select(User.id).where(User.telegram_id.in_([xss_tg, blocked_tg, non_admin_tg]))
                        )
                    ).scalars().all()
                )
                if uids:
                    await session.execute(delete(User).where(User.id.in_(uids)))
                await session.execute(delete(Product).where(Product.id == prod.id))
                await session.execute(delete(Category).where(Category.id == cat.id))
        for uid in (xss_tg, blocked_tg, non_admin_tg):
            await redis_client.delete(f"rl:win:{uid}", f"rl:alert:{uid}")


def test_static_repo_security_and_reliability_scans(project_root: Path) -> None:
    assert subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=project_root, check=False).returncode == 0
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert ".env" not in tracked

    float_col_re = re.compile(r"\b(?:sa\.)?(?:Float|REAL|DOUBLE_PRECISION)\b")
    for rel in ("app/database/models.py", "migrations/versions/0001_initial_schema.py"):
        content = (project_root / rel).read_text(encoding="utf-8")
        assert not float_col_re.search(content), f"Float DB column found in {rel}"

    # Zero float() monetary conversions in domain services (only broadcast FloodWait/rate-pacing allowed)
    allowed_float_paths = {("app/services/broadcast_service.py", "wait_seconds"), ("app/services/broadcast_service.py", "effective_rate")}
    for svc_file in (project_root / "app" / "services").rglob("*.py"):
        rel_path = svc_file.relative_to(project_root).as_posix()
        for lineno, line in enumerate(svc_file.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if "float(" in stripped and not stripped.startswith("#"):
                if any(rel_path == p and var in stripped for p, var in allowed_float_paths):
                    continue
                raise AssertionError(f"float() call found in {svc_file}:{lineno}: {stripped}")

    stub_re = re.compile(r"\b(?:TO" + r"DO|FIX" + r"ME|NotImplemented" + r"Error)\b")
    bare_except_re = re.compile(r"^\s*except\s*:")
    secret_re_list = [
        re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}\b"),
        re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{20,}\b"),
        re.compile(r"(?:postgresql(?:\+asyncpg)?|redis)://(?!\{)[^:\s'\"{}]+:(?!\{)[^@\s'\"{}]+@(?!127\.0\.0\.1|localhost|\{)"),
    ]
    for py_file in (project_root / "app").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert not stub_re.search(text), f"Stub marker in {py_file}"
        for lineno, line in enumerate(text.splitlines(), start=1):
            assert not bare_except_re.match(line), f"Bare except: in {py_file}:{lineno}"
        for pat in secret_re_list:
            assert not pat.search(text), f"Potential hardcoded secret in {py_file}"
