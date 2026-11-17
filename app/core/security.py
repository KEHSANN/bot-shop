"""Cryptographic, authentication, CSRF, HTML escaping, and upload validation helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import struct
import time
from typing import Any
from urllib.parse import parse_qsl

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.constants import ALLOWED_RECEIPT_MIME_TYPES, MAX_RECEIPT_FILE_BYTES
from app.core.exceptions import AuthenticationError, EncryptionError, FileValidationError


def generate_fernet_key() -> str:
    """Generate a fresh URL-safe base64 Fernet encryption key."""
    return Fernet.generate_key().decode("utf-8")


def _get_fernet(encryption_key: str) -> Fernet:
    if not encryption_key or not isinstance(encryption_key, str):
        raise EncryptionError("Encryption key must be a non-empty string")
    try:
        return Fernet(encryption_key.encode("utf-8"))
    except Exception as exc:
        raise EncryptionError("Invalid Fernet encryption key format") from exc


def encrypt_secret(plaintext: str, encryption_key: str) -> str:
    """Encrypt sensitive credentials (stock payloads, merchant IDs) at rest with Fernet."""
    if plaintext is None:
        raise EncryptionError("Cannot encrypt None value")
    fernet = _get_fernet(encryption_key)
    token = fernet.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(ciphertext: str, encryption_key: str) -> str:
    """Decrypt a Fernet-encrypted credential payload."""
    if not ciphertext:
        raise EncryptionError("Cannot decrypt empty ciphertext")
    fernet = _get_fernet(encryption_key)
    try:
        raw = fernet.decrypt(ciphertext.encode("utf-8"))
        return raw.decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError("Failed to decrypt payload: invalid token or key") from exc
    except Exception as exc:
        raise EncryptionError("Unexpected decryption failure") from exc


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt with per-password random salt."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify plaintext password against stored bcrypt hash in constant time."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(
    subject: str,
    secret_key: str,
    *,
    expires_minutes: int = 720,
    extra_claims: dict[str, Any] | None = None,
    algorithm: str = "HS256",
) -> str:
    """Create a signed JWT access token."""
    if not secret_key or len(secret_key) < 16:
        raise AuthenticationError("JWT secret key is missing or too short")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "exp": now + (expires_minutes * 60),
        "jti": secrets.token_hex(12),
    }
    if extra_claims:
        for k, v in extra_claims.items():
            if k not in {"sub", "iat", "exp", "jti"}:
                payload[k] = v
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def verify_access_token(token: str, secret_key: str, *, algorithm: str = "HS256") -> dict[str, Any]:
    """Verify JWT signature and expiration; return decoded claims."""
    if not token or not secret_key:
        raise AuthenticationError("Missing authentication token or secret key")
    try:
        claims = jwt.decode(token, secret_key, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Authentication token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token") from exc
    if "sub" not in claims:
        raise AuthenticationError("Authentication token missing subject")
    return claims


def generate_csrf_token(secret_key: str, session_id: str) -> str:
    """Generate an HMAC-SHA256 CSRF token bound to session_id and issued timestamp."""
    if not secret_key or not session_id:
        raise AuthenticationError("CSRF secret_key and session_id are required")
    issued_at = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    message = f"{session_id}:{issued_at}:{nonce}".encode("utf-8")
    sig = hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{issued_at}.{nonce}.{sig}"


def verify_csrf_token(
    token: str,
    secret_key: str,
    session_id: str,
    *,
    max_age_seconds: int = 43200,
) -> bool:
    """Validate an HMAC-SHA256 CSRF token in constant time."""
    if not token or not secret_key or not session_id:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    issued_at_str, nonce, provided_sig = parts
    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return False
    now = int(time.time())
    if issued_at > now + 60 or (now - issued_at) > max_age_seconds:
        return False
    message = f"{session_id}:{issued_at_str}:{nonce}".encode("utf-8")
    expected_sig = hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_sig, expected_sig)


def verify_telegram_login_widget(
    auth_data: dict[str, Any],
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> bool:
    """Verify Telegram Login Widget payload using SHA256(bot_token) HMAC-SHA256."""
    if not bot_token or not auth_data or "hash" not in auth_data or "auth_date" not in auth_data:
        return False
    provided_hash = str(auth_data["hash"])
    try:
        auth_date = int(auth_data["auth_date"])
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if auth_date > now + 60 or (now - auth_date) > max_age_seconds:
        return False

    check_items = [
        f"{k}={v}"
        for k, v in sorted(auth_data.items())
        if k != "hash" and v is not None and v != ""
    ]
    data_check_string = "\n".join(check_items).encode("utf-8")
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_hash, expected_hash)


def verify_telegram_webapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> dict[str, Any]:
    """Verify Telegram Mini App WebApp.initData HMAC-SHA256 signature and return parsed data."""
    if not init_data or not bot_token:
        raise AuthenticationError("Missing WebApp initData or bot token")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = pairs.pop("hash", "")
    if not provided_hash or "auth_date" not in pairs:
        raise AuthenticationError("Invalid WebApp initData payload")
    try:
        auth_date = int(pairs["auth_date"])
    except ValueError as exc:
        raise AuthenticationError("Invalid WebApp auth_date") from exc
    now = int(time.time())
    if auth_date > now + 60 or (now - auth_date) > max_age_seconds:
        raise AuthenticationError("WebApp initData expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items())).encode("utf-8")
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_hash, expected_hash):
        raise AuthenticationError("Invalid WebApp initData signature")

    result: dict[str, Any] = dict(pairs)
    if "user" in result:
        try:
            result["user"] = json.loads(result["user"])
        except json.JSONDecodeError as exc:
            raise AuthenticationError("Malformed WebApp user JSON") from exc
    return result


def verify_totp_code(secret_b32: str, code: str, *, window: int = 1) -> bool:
    """Verify an RFC 6238 6-digit TOTP code for optional Web Admin 2FA."""
    if not secret_b32 or not code or not code.isdigit() or len(code) != 6:
        return False
    try:
        padded = secret_b32.strip().upper().replace(" ", "")
        padded += "=" * ((8 - len(padded) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
    except Exception:
        return False

    counter = int(time.time()) // 30
    for offset in range(-window, window + 1):
        msg = struct.pack(">Q", counter + offset)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        pos = digest[-1] & 0x0F
        truncated = struct.unpack(">I", digest[pos : pos + 4])[0] & 0x7FFFFFFF
        otp = f"{truncated % 1_000_000:06d}"
        if hmac.compare_digest(otp, code):
            return True
    return False


def escape_html(text: str | None) -> str:
    """Escape HTML entities strictly before Telegram HTML parse mode or web rendering."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def sanitize_username(username: str | None) -> str:
    """Return HTML-escaped @username or '-' when absent."""
    if not username:
        return "-"
    cleaned = username.lstrip("@").strip()
    return f"@{escape_html(cleaned)}" if cleaned else "-"


def sha256_hex(text: str) -> str:
    """Compute SHA-256 hex digest of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_image_mime(data: bytes) -> str | None:
    """Inspect magic bytes of an image buffer and return MIME type or None."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_receipt_image(
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    max_bytes: int = MAX_RECEIPT_FILE_BYTES,
) -> str:
    """Validate uploaded payment receipt image by size, magic bytes, and optional MIME/extension."""
    if not data:
        raise FileValidationError("Uploaded file is empty")
    if len(data) > max_bytes:
        raise FileValidationError(f"File exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB")

    detected_mime = detect_image_mime(data)
    if detected_mime not in ALLOWED_RECEIPT_MIME_TYPES:
        raise FileValidationError("Unsupported image format; only JPEG, PNG, and WEBP images are allowed")

    if content_type:
        normalized_ct = content_type.split(";")[0].strip().lower()
        if normalized_ct == "image/jpg":
            normalized_ct = "image/jpeg"
        if normalized_ct not in ALLOWED_RECEIPT_MIME_TYPES or normalized_ct != detected_mime:
            raise FileValidationError(
                f"Content-Type '{content_type}' does not match detected image signature '{detected_mime}'"
            )

    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        allowed_exts = {
            "image/jpeg": {"jpg", "jpeg"},
            "image/png": {"png"},
            "image/webp": {"webp"},
        }[detected_mime]
        if ext and ext not in allowed_exts:
            raise FileValidationError(f"File extension '.{ext}' does not match detected '{detected_mime}'")

    return detected_mime


def validate_stock_upload_file(
    data: bytes,
    *,
    filename: str | None = None,
    max_bytes: int = MAX_RECEIPT_FILE_BYTES,
) -> list[str]:
    """Validate and parse a UTF-8 .txt/.csv bulk stock upload file into non-empty stripped lines."""
    if not data:
        raise FileValidationError("Stock upload file is empty")
    if len(data) > max_bytes:
        raise FileValidationError(f"Stock file exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB")
    if b"\x00" in data:
        raise FileValidationError("Binary content is not allowed in stock upload files")
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and ext not in {"txt", "csv"}:
            raise FileValidationError("Only .txt or .csv files are supported for bulk stock upload")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileValidationError("Stock upload file must be valid UTF-8 text") from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise FileValidationError("Stock upload file contains no non-empty items")
    return lines
