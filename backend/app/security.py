"""密码、登录令牌（JWT）、托管 Key 的加密。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_SCRYPT = dict(n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(plain: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(plain.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(h).decode()}"


def verify_password(plain: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        kind, salt_b, hash_b = stored.split("$")
        if kind != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b)
        want = base64.urlsafe_b64decode(hash_b)
    except (ValueError, TypeError):
        return False
    got = hashlib.scrypt(plain.encode(), salt=salt, **_SCRYPT)
    return hmac.compare_digest(want, got)


def make_jwt(user_id: str, role: str, hours: int | None = None) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": now + 3600 * (hours or settings.jwt_hours)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def parse_jwt(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def _kek() -> bytes:
    raw = settings.key_encryption_key.strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = hashlib.sha256(raw.encode()).digest()
        if len(key) != 32:
            key = hashlib.sha256(key).digest()
        return key
    # 开发环境：从 jwt_secret 派生；生产必须单独配置 KEY_ENCRYPTION_KEY
    return hashlib.sha256(b"token-market-kek:" + settings.jwt_secret.encode()).digest()


def seal(plain: str) -> bytes:
    """AES-256-GCM：v1 || 12 字节 nonce || 密文+tag。"""
    nonce = os.urandom(12)
    ct = AESGCM(_kek()).encrypt(nonce, plain.encode(), b"v1")
    return b"v1" + nonce + ct


def unseal(blob: bytes | memoryview) -> str:
    b = bytes(blob)
    if b[:2] != b"v1" or len(b) < 2 + 12 + 16:
        raise ValueError("密文格式不对")
    return AESGCM(_kek()).decrypt(b[2:14], b[14:], b"v1").decode()


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()[:16]


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def random_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def random_password(length: int = 24) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length)) + "!a1"
