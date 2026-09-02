"""Trusted-device recognition and the new-device email OTP.

This is the *fallback* second factor, not an extra one. It applies only when
an account has neither a passkey nor an authenticator enrolled, and only in
production — in development a new browser signs in on the password alone, or
nobody could log into a fresh checkout without a working SMTP relay.

The shape is the familiar one: a successful sign-in leaves a long-lived,
HttpOnly cookie holding a random secret; only its SHA-256 is stored, so
reading the table does not yield a usable device token. A browser that
presents a live, unrevoked token skips the challenge. A browser that does not
gets a six-digit code emailed to the account, and only proves out by entering
it.

The code lives in Redis, never in the database, keyed by an opaque challenge
id: it expires on its own, survives no restart it shouldn't, and cannot be
read back out of a database dump.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

import redis
from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TrustedDevice, User, WebAuthnCredential, utcnow
from app.rate_limit import get_redis
from app.services.mailer import send_email

log = logging.getLogger("papertick.devices")

DEVICE_COOKIE = "pt_device"
CODE_LENGTH = 6
MAX_ATTEMPTS = 5


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def cookie_path() -> str:
    return get_settings().base_path or "/"


# --------------------------------------------------------------- applicability

def has_second_factor(db: Session, user: User) -> bool:
    """True when the account already carries something stronger than a password."""
    if user.mfa_enabled:
        return True
    return db.execute(
        select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == user.id).limit(1)
    ).first() is not None


def verification_required(db: Session, user: User, request: Request) -> bool:
    """Does this sign-in need a new-device code?

    No in development, no when the deployment has turned it off, no when the
    account has a real second factor, and no when the browser presents a token
    this account has used before.
    """
    s = get_settings()
    if not (s.device_verification and s.is_production):
        return False
    if has_second_factor(db, user):
        return False
    return not is_trusted(db, user, request.cookies.get(DEVICE_COOKIE))


# ------------------------------------------------------------------- the cookie

def is_trusted(db: Session, user: User, raw: str | None) -> bool:
    if not raw:
        return False
    row = db.execute(
        select(TrustedDevice).where(
            TrustedDevice.user_id == user.id,
            TrustedDevice.token_hash == _hash(raw),
            TrustedDevice.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at <= utcnow():
        return False
    row.last_seen_at = utcnow()
    db.commit()
    return True


def _label(request: Request) -> str:
    """A human-recognisable name for the row, from the User-Agent.

    Deliberately coarse — browser and platform family, nothing more. This
    exists so the user can tell one row from another in Settings, not to
    fingerprint the device.
    """
    ua = (request.headers.get("user-agent") or "")[:400]
    browser = next((b for b in ("Edg", "OPR", "Chrome", "Firefox", "Safari") if b in ua), None)
    browser = {"Edg": "Edge", "OPR": "Opera"}.get(browser, browser) or "Browser"
    platform = next(
        (p for p in ("Windows", "Macintosh", "iPhone", "iPad", "Android", "Linux") if p in ua),
        "Unknown device",
    )
    return f"{browser} on {'macOS' if platform == 'Macintosh' else platform}"[:120]


def remember(db: Session, user: User, request: Request, response: Response) -> None:
    """Mint a device token for this browser and set the cookie."""
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    ttl = timedelta(days=s.device_trust_days)
    from app.rate_limit import client_ip

    db.add(TrustedDevice(
        user_id=user.id,
        token_hash=_hash(raw),
        label=_label(request),
        last_ip=client_ip(request)[:45],
        expires_at=utcnow() + ttl,
        last_seen_at=utcnow(),
    ))
    db.commit()
    response.set_cookie(
        DEVICE_COOKIE, raw,
        max_age=int(ttl.total_seconds()), httponly=True,
        samesite="lax", secure=s.cookie_secure, path=cookie_path(),
    )


def forget_cookie(response: Response) -> None:
    response.delete_cookie(DEVICE_COOKIE, path=cookie_path())


def list_for(db: Session, user: User) -> list[TrustedDevice]:
    return list(db.execute(
        select(TrustedDevice)
        .where(TrustedDevice.user_id == user.id, TrustedDevice.revoked_at.is_(None))
        .order_by(TrustedDevice.created_at.desc())
    ).scalars())


def revoke(db: Session, user: User, device_id: str) -> bool:
    row = db.get(TrustedDevice, device_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    db.commit()
    return True


def revoke_all(db: Session, user: User) -> int:
    rows = list_for(db, user)
    for row in rows:
        row.revoked_at = utcnow()
    db.commit()
    return len(rows)


# ---------------------------------------------------------------- the challenge

def _key(challenge_id: str) -> str:
    return f"devotp:{challenge_id}"


def start_challenge(db: Session, user: User, request: Request) -> str:
    """Email a one-time code and return the id that redeems it."""
    s = get_settings()
    challenge_id = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
    try:
        get_redis().hset(_key(challenge_id), mapping={
            "user": user.id,
            "code": _hash(code),
            "attempts": "0",
        })
        get_redis().expire(_key(challenge_id), s.device_otp_ttl_seconds)
    except redis.RedisError:
        log.error("device OTP store unavailable")
        raise
    minutes = max(1, s.device_otp_ttl_seconds // 60)
    send_email(
        user.email,
        "Your PaperTick sign-in code",
        f"Someone signed in to PaperTick from a device we don't recognise "
        f"({_label(request)}).\n\n"
        f"Your one-time code is: {code}\n\n"
        f"It expires in {minutes} minutes. If this wasn't you, change your "
        f"password — and consider adding a passkey or an authenticator app, "
        f"which replace this step entirely.",
    )
    log.info("new-device code issued for %s", user.id)
    return challenge_id


def verify_challenge(challenge_id: str, code: str) -> str | None:
    """User id on success, None otherwise. Burns the challenge either way once
    the attempt budget is gone, so a code cannot be brute-forced inside its
    lifetime."""
    key = _key(challenge_id)
    try:
        r = get_redis()
        data = r.hgetall(key)
        if not data:
            return None
        attempts = int(data.get("attempts", "0")) + 1
        if attempts > MAX_ATTEMPTS:
            r.delete(key)
            return None
        if not secrets.compare_digest(data.get("code", ""), _hash(code.strip())):
            r.hset(key, "attempts", str(attempts))
            return None
        r.delete(key)
        return data.get("user")
    except redis.RedisError:
        log.error("device OTP store unavailable")
        return None
