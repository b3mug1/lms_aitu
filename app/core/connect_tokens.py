from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import NamedTuple


def _get_signing_key(key_str: str | None) -> bytes:
    if not key_str:
        return b"aitu-default-connect-signing-key-2026"
    return key_str.encode("utf-8")


def create_connect_token(
    telegram_user_id: int,
    signing_key: str | None = None,
    ttl_seconds: int = 1800,
) -> tuple[str, str]:
    """Generate a stateless HMAC signed token and a short 6-digit PIN."""
    now = int(time.time())
    expires_at = now + ttl_seconds
    key = _get_signing_key(signing_key)

    # 1. URL Token
    raw_payload = f"{telegram_user_id}:{expires_at}"
    sig = hmac.new(key, raw_payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    full_str = f"{raw_payload}:{sig}"
    token = base64.urlsafe_b64encode(full_str.encode("utf-8")).decode("ascii").rstrip("=")

    # 2. 6-digit PIN code
    code_raw = f"pin:{telegram_user_id}:{now // 600}"
    code_hash = hmac.new(key, code_raw.encode("utf-8"), hashlib.sha256).hexdigest()
    code_num = (int(code_hash[:8], 16) % 900000) + 100000
    code = str(code_num)

    return token, code


def resolve_connect_token(
    token_or_code: str,
    signing_key: str | None = None,
) -> int | None:
    """Validate token and return telegram_user_id if valid."""
    cleaned = token_or_code.strip()
    if not cleaned:
        return None

    key = _get_signing_key(signing_key)
    now = int(time.time())

    try:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = decoded.split(":")
        if len(parts) == 3:
            uid_str, exp_str, sig = parts
            telegram_user_id = int(uid_str)
            expires_at = int(exp_str)

            if expires_at < now:
                return None

            expected_sig = hmac.new(
                key, f"{telegram_user_id}:{expires_at}".encode("utf-8"), hashlib.sha256
            ).hexdigest()[:16]

            if hmac.compare_digest(sig, expected_sig):
                return telegram_user_id
    except Exception:
        pass

    return None
