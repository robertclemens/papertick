"""Cryptographic primitives: Argon2id passwords, JWTs, API keys, TOTP secret sealing.

All primitives come from vetted libraries (argon2-cffi, PyJWT, cryptography, pyotp).
Secrets are handled as short-lived locals; helper `wipe()` best-effort zeroes
mutable buffers (CPython cannot guarantee immutable str cleanup — documented limit).
"""

import base64
import hashlib
import hmac
import json
import logging
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

log = logging.getLogger("papertick.security")

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

def _derive_key(salt: bytes, info: bytes) -> bytearray:
    """A 32-byte subkey of SECRET_KEY, domain-separated by (salt, info).

    Returned as a bytearray so the caller can zero it after use — `wipe()` on a
    `bytes` copy would zero the copy and leave the original in memory.
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info)
    return bytearray(hkdf.derive(get_settings().secret_key.encode()))


def _fernet() -> Fernet:
    key = _derive_key(b"papertick.totp.v1", b"totp-secret-encryption")
    fkey = base64.urlsafe_b64encode(bytes(key))
    try:
        return Fernet(fkey)
    finally:
        wipe(key)
        wipe(bytearray(fkey))


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


# ---------------------------------------------------------------- export signing

def _canonical(payload: dict) -> bytes:
    """Stable byte rendering of an export body, so a signature does not depend
    on key order or whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def sign_export(payload: dict) -> str:
    """Detached HMAC-SHA256 over an export body.

    Scenario import writes straight into the ledger — balances, contributions,
    realized gains and tax lots. Without a signature the endpoint is an
    arbitrary-state-injection primitive: a hand-written file can mint any
    balance and bypass every contribution limit. Signing makes the export a
    restore artefact rather than an authoring format; a genuine export
    round-trips untouched, an edited one does not verify.
    """
    key = _derive_key(b"papertick.export.v1", b"scenario-export-signing")
    try:
        return hmac.new(bytes(key), _canonical(payload), hashlib.sha256).hexdigest()
    finally:
        wipe(key)


def verify_export(payload: dict, signature: str | None) -> bool:
    if not signature or not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign_export(payload), signature)


# ---------------------------------------------------------------- access-token revocation

def _revoked_key(jti: str) -> str:
    return f"revoked:jti:{jti}"


def revoke_access_token(token: str) -> None:
    """Deny a still-valid access JWT for the rest of its lifetime.

    Access tokens are stateless, so signing out or changing a password
    otherwise leaves a stolen one working until it expires — "sign out
    everywhere" that does not. The entry expires with the token, so the
    denylist stays small.
    """
    from app.rate_limit import get_redis

    payload = decode_token(token, "access")
    if payload is None:
        return
    ttl = int(payload["exp"] - datetime.now(timezone.utc).timestamp())
    if ttl <= 0:
        return
    try:
        get_redis().set(_revoked_key(payload["jti"]), "1", ex=ttl)
    except Exception as exc:  # noqa: BLE001 — a store outage must not break sign-out
        log.error("could not record token revocation: %s", exc.__class__.__name__)


def revoke_all_access_tokens(user_id: str) -> None:
    """Deny every access token issued to `user_id` before now.

    Individual jtis are not enumerable, so this records a cutoff instead: any
    token issued at or before this instant is refused. Kept for the maximum
    lifetime of an access token, after which nothing older can still be valid.
    """
    from app.rate_limit import get_redis

    ttl = get_settings().access_token_ttl_seconds + 60
    try:
        get_redis().set(f"revoked:before:{user_id}",
                        str(datetime.now(timezone.utc).timestamp()), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        log.error("could not record session cutoff for %s: %s", user_id, exc.__class__.__name__)


def access_token_revoked(payload: dict) -> bool:
    from app.rate_limit import get_redis

    try:
        r = get_redis()
        if r.get(_revoked_key(payload.get("jti", ""))):
            return True
        cutoff = r.get(f"revoked:before:{payload.get('sub', '')}")
        return cutoff is not None and float(payload.get("iat", 0)) <= float(cutoff)
    except Exception as exc:  # noqa: BLE001
        # Fail open: the token is still signed, unexpired and issued by us.
        # Refusing every request when the denylist is unreachable would turn a
        # cache outage into a total sign-out.
        log.error("revocation check unavailable: %s", exc.__class__.__name__)
        return False
