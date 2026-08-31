"""Cryptographic primitives: Argon2id passwords, JWTs, API keys, TOTP secret sealing.

All primitives come from vetted libraries (argon2-cffi, PyJWT, cryptography, pyotp).
Secrets are handled as short-lived locals; helper `wipe()` best-effort zeroes
mutable buffers (CPython cannot guarantee immutable str cleanup — documented limit).
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

# Argon2id, tuned above library defaults (64 MiB memory hard).
_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)

API_KEY_PREFIX = "ptk_"
JWT_ALG = "HS256"
JWT_ISSUER = "papertick"

COMMON_PASSWORDS = {
    "password", "password1", "password123", "letmein", "qwerty123456",
    "123456789012", "iloveyou12345", "adminadmin12", "welcome12345",
}


def wipe(buf: bytearray) -> None:
    """Best-effort zeroization of a mutable secret buffer."""
    for i in range(len(buf)):
        buf[i] = 0


# ---------------------------------------------------------------- passwords

def validate_password_strength(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    if len(password) > 256:
        raise ValueError("Password must be at most 256 characters")
    if password.lower() in COMMON_PASSWORDS:
        raise ValueError("Password is too common")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("Password must contain both letters and digits")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


# ---------------------------------------------------------------- JWT

def _make_token(sub: str, token_type: str, ttl_seconds: int, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "type": token_type,
        "jti": secrets.token_hex(16),
        "iss": JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALG)


def make_access_token(user_id: str) -> str:
    return _make_token(user_id, "access", get_settings().access_token_ttl_seconds)


def make_mfa_token(user_id: str) -> str:
    return _make_token(user_id, "mfa", get_settings().mfa_token_ttl_seconds)


def make_email_verify_token(user_id: str) -> str:
    return _make_token(user_id, "email_verify", get_settings().email_token_ttl_seconds)


def make_email_change_token(user_id: str, new_email: str) -> str:
    return _make_token(
        user_id, "email_change", get_settings().email_token_ttl_seconds,
        extra={"new_email": new_email},
    )


def decode_token(token: str, expected_type: str) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[JWT_ALG],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "sub", "type", "iss"]},
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload


# ---------------------------------------------------------------- refresh tokens

def new_refresh_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hash). Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- API keys

def new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext, sha256_hash, display_prefix)."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(36)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:12]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------- TOTP (secret sealed at rest)

def _fernet() -> Fernet:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"papertick.totp.v1",
        info=b"totp-secret-encryption",
    )
    key = hkdf.derive(get_settings().secret_key.encode())
    fkey = base64.urlsafe_b64encode(key)
    f = Fernet(fkey)
    wipe(bytearray(key))
    return f


def new_totp_secret() -> str:
    return pyotp.random_base32()


def seal_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def open_totp_secret(sealed: str) -> str | None:
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="PaperTick")


def verify_totp(secret: str, code: str) -> bool:
    if not code or not code.strip().isdigit():
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
