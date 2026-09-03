"""Forgotten-password recovery.

The threat here is not a user who forgot a password; it is everyone else. A
reset flow is the shortest path to account takeover in most applications, so
each step is closed deliberately:

  * **The token is not guessable.** 256 bits from `secrets.token_urlsafe(32)`.
    There is no code to brute-force and no identifier to enumerate — the token
    is the only thing that identifies the request.
  * **Only its hash is stored.** A database read (or a leaked backup) yields
    nothing that can be redeemed, exactly as for refresh and device tokens.
  * **It is single-use and short-lived.** Redeeming one marks it used *and*
    burns every other outstanding token for that user, so a link forwarded,
    logged by a mail scanner, or sitting in a browser history cannot be
    replayed after the fact.
  * **Requesting one changes nothing.** The password stands until a token is
    redeemed, so an attacker spamming requests only fills an inbox — and a
    *completed* sign-in burns outstanding tokens too, which is what makes "if
    this wasn't you, ignore it" true. Completed, not merely a correct password:
    otherwise someone holding a stolen password but not the second factor could
    cancel the owner's recovery links on demand and close the one route back in.
  * **The response never varies.** Requesting a reset for an address that does
    not exist returns exactly what a real one returns, at the same cost, so
    the endpoint cannot be used to test which addresses are registered.
  * **Redeeming is a full session reset.** Every refresh token, every access
    token and every remembered device is cleared: if the reset was the
    attacker's, the legitimate user's session dies too, and vice versa —
    whoever holds the mailbox wins, which is the intended answer.

Rate limiting is applied by the router on two keys at once, per source address
and per target address, because either one alone is bypassable: a botnet
rotates addresses, and one address can hammer many accounts.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PasswordResetToken, RefreshToken, User, as_utc, utcnow

log = logging.getLogger("papertick.password_reset")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def burn_outstanding(db: Session, user_id: str) -> int:
    """Invalidate every unused reset token for a user. Caller commits.

    Called when one is redeemed, and whenever the password changes by another
    route — a link minted before the change must not still work after it.
    """
    return db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": utcnow()}, synchronize_session=False)


def issue(db: Session, user: User, ip: str) -> str:
    """Mint a reset token for `user` and return the plaintext. Caller commits.

    Any previously outstanding token is burned first, so at most one live link
    exists per account and the most recent request is the one that works.
    """
    burn_outstanding(db, user.id)
    raw = secrets.token_urlsafe(32)
    ttl = get_settings().password_reset_ttl_seconds
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=_hash(raw),
        expires_at=utcnow() + timedelta(seconds=ttl),
        requested_ip=ip[:45],
    ))
    log.info("password reset requested for %s", user.id)
    return raw


def redeem(db: Session, raw: str) -> User | None:
    """The user a live token belongs to, marking it used. None if it is not
    redeemable — unknown, already used, or expired, which are deliberately
    indistinguishable to the caller. Caller commits.
    """
    if not raw:
        return None
    row = db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash(raw))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if as_utc(row.expires_at) <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.used_at = utcnow()
    # Everything else outstanding dies with it: one reset, one link.
    burn_outstanding(db, user.id)
    return user


def clear_sessions(db: Session, user: User) -> None:
    """Sign out everywhere and forget every remembered browser. Caller commits.

    A reset means the account was, or may have been, out of the owner's
    control. Leaving a live session or a trusted-device cookie behind would
    leave the attacker exactly the access the reset was meant to remove.
    """
    from app.security import revoke_all_access_tokens
    from app.services import devices

    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ).update({"revoked_at": utcnow()}, synchronize_session=False)
    devices.revoke_all(db, user)
    revoke_all_access_tokens(user.id)
