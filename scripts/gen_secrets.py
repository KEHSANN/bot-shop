#!/usr/bin/env python3
"""Generate cryptographic secrets, Fernet encryption key, and optional .env file."""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path


def _generate_fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _hash_password_bcrypt(password: str) -> str:
    try:
        import bcrypt
    except ImportError as exc:
        raise RuntimeError(
            "The 'bcrypt' package is required to hash WEB_ADMIN_PASSWORD_HASH. "
            "Run with the project virtualenv: .venv/bin/python scripts/gen_secrets.py"
        ) from exc
    if len(password) < 8:
        raise ValueError("Admin password must be at least 8 characters long")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate production secrets for .env")
    parser.add_argument("--write-env", action="store_true", help="Write generated values into .env from .env.example")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .env if --write-env is used")
    parser.add_argument("--admin-password", default="", help="Web Admin plaintext password to bcrypt-hash (generated if omitted)")
    parser.add_argument("--domain", default="shop.example.com", help="Public domain name for webhook and Mini App URLs")
    parser.add_argument("--bot-mode", choices=["webhook", "polling"], default="webhook", help="Telegram Bot mode")
    args = parser.parse_args()

    admin_pw = args.admin_password or secrets.token_urlsafe(16)
    pw_hash = _hash_password_bcrypt(admin_pw)
    db_pass = secrets.token_hex(16)
    redis_pass = secrets.token_hex(16)

    generated = {
        "DOMAIN": args.domain,
        "BOT_MODE": args.bot_mode,
        "BOT_WEBHOOK_URL": f"https://{args.domain}/webhook/telegram",
        "BOT_WEBHOOK_SECRET": secrets.token_hex(24),
        "WEBAPP_URL": f"https://{args.domain}/miniapp",
        "MINIAPP_BASE_URL": f"https://{args.domain}/miniapp",
        "WEB_ADMIN_PASSWORD_HASH": f"'{pw_hash}'",
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "CSRF_SECRET_KEY": secrets.token_hex(32),
        "ADMIN_SECRET_KEY": secrets.token_hex(32),
        "ENCRYPTION_KEY": _generate_fernet_key(),
        "POSTGRES_PASSWORD": db_pass,
        "DATABASE_URL": f"postgresql+asyncpg://shopbot:{db_pass}@127.0.0.1:5432/telegram_shop",
        "REDIS_PASSWORD": redis_pass,
        "REDIS_URL": f"redis://:{redis_pass}@127.0.0.1:6379/0",
    }

    if not args.write_env:
        for k, v in generated.items():
            print(f"{k}={v}")
        if not args.admin_password:
            print(f"# Generated initial WEB_ADMIN plaintext password: {admin_pw}", file=sys.stderr)
        return 0

    root = Path(__file__).resolve().parent.parent
    example_path = root / ".env.example"
    env_path = root / ".env"
    if not example_path.exists():
        print("ERROR: .env.example not found", file=sys.stderr)
        return 1
    if env_path.exists() and not args.force:
        print("ERROR: .env already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    lines = example_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in generated:
                out_lines.append(f"{key}={generated[key]}")
                continue
        out_lines.append(line)

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    print(f"Wrote {env_path} with mode 0600.")
    if not args.admin_password:
        print(f"Initial Web Admin password (save securely, not stored in .env): {admin_pw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
