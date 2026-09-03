"""Trusted-device recognition and the new-device email OTP.

This is what the *password* path proves on an unrecognised browser, and it
applies in production to every password sign-in that cannot show a device
token — including accounts that also hold passkeys. Enrolling a passkey makes
the passkey route available; it does not make a password sign-in stronger. In
development it is skipped entirely, or nobody could log into a fresh checkout
without a working SMTP relay.

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
from app.models import TrustedDevice, User, WebAuthnCredential, as_utc, utcnow
from app.rate_limit import get_redis
from app.services.audit import device_label
from app.services.mailer import send_rendered, utc_stamp

log = logging.getLogger("papertick.devices")

DEVICE_COOKIE = "pt_device"
CODE_LENGTH = 6
MAX_ATTEMPTS = 5


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def cookie_path() -> str:
    return get_settings().base_path or "/"


# --------------------------------------------------------------- applicability

def has_passkey(db: Session, user: User) -> bool:
    """Whether the account has any passkey enrolled.

    Deliberately *not* consulted when deciding whether a sign-in needs a
    device code: see `verification_required`.
    """
    return db.execute(
        select(WebAuthnCredential.id).where(WebAuthnCredential.user_id == user.id).limit(1)
    ).first() is not None


def verification_required(db: Session, user: User, request: Request) -> bool:
    """Does this password sign-in need a new-device code?

    No in development, no when the deployment has turned it off, and no when
    the browser presents a device token this account has used before.
    Otherwise: yes.

    What is deliberately *not* a reason to skip it is the account having a
    passkey or an authenticator enrolled. This used to check exactly that, and
    it is a factor-confusion bug: a credential the account *could* have used
    is not a credential this sign-in *did* use. An account with one passkey
    signed in on the password alone, from a browser nobody had ever seen,
    with no code and no email — the passkey's strength was credited to a
    sign-in that never touched it.

    Only callers on the password path reach here, and only after the factor
    they actually presented has been checked. TOTP is handled before this by
    `/login` returning `mfa_required`, so a code really was entered; a passkey
    sign-in goes through its own route and never asks. Here the sole evidence
    is a password, so an unrecognised browser gets a code.
    """
    s = get_settings()
    if not (s.device_verification and s.is_production):
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
    if row is None or as_utc(row.expires_at) <= utcnow():
        return False
    row.last_seen_at = utcnow()
    db.commit()
    return True


def remember(db: Session, user: User, request: Request, response: Response) -> None:
    """Mint a device token for this browser and set the cookie."""
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    ttl = timedelta(days=s.device_trust_days)
    from app.rate_limit import client_ip

    db.add(TrustedDevice(
        user_id=user.id,
        token_hash=_hash(raw),
        label=device_label(request),
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
    from app.models import SecurityEventKind
    from app.services import audit

    audit.record(db, user, SecurityEventKind.DEVICE_TRUSTED, request)
    db.commit()


def forget_cookie(response: Response) -> None:
    # Mirror the attributes `remember` sets, for the same reason `_clear_cookies`
    # does: a delete whose attributes disagree with the original is not
    # guaranteed to land. This cookie carries no `__Host-`/`__Secure-` prefix,
    # so today's asymmetry still expires it — keep them in step anyway.
    response.delete_cookie(
        DEVICE_COOKIE, path=cookie_path(),
        httponly=True, samesite="lax", secure=get_settings().cookie_secure,
    )


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
    from app.rate_limit import client_ip

    send_rendered(
        user.email, "Your PaperTick sign-in code",
        heading=f"Your sign-in code is {code}",
        lede=("Someone signed in to PaperTick with your password from a browser we "
              "don't recognise. Enter this code to finish signing in."),
        rows=[("Code", code),
              ("Expires in", f"{minutes} minutes"),
              ("When", utc_stamp()),
              ("IP address", client_ip(request)),
              ("Device", device_label(request))],
        closing=("If this wasn't you, someone has your password: reset it now and review "
                 "your security activity in Settings. We will never ask you for this code."),
        alert=True,
    )
    from app.models import SecurityEventKind
    from app.services import audit

    audit.record(db, user, SecurityEventKind.DEVICE_CODE_SENT, request)
    db.commit()
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
