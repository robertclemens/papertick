"""The account security log, and the emails that mirror it.

Two things happen for every security-relevant action: a row is written, and
(for the ones a user would want to know about) a notice is sent. The row is
written first and independently, so the trail survives a mail relay being
down — an account takeover is exactly when outbound email is most likely to
be broken, and a log that only exists when SMTP works is not a log.

**The originating IP.** The whole point of the table is answering "where did
this come from", so the address has to be the real one. It is resolved by
`rate_limit.client_ip`, which walks X-Forwarded-For from the right and stops
at the first hop it did not put there — behind Caddy that needs
TRUSTED_PROXY_CIDRS set, or every row reads as the proxy's own address. With
the frontend exposed directly there is no proxy and the peer address is
already correct, so the setting must then be left empty: trusting a CIDR that
is not in front of you lets a caller name its own IP.
"""

import logging

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SecurityEvent, SecurityEventKind, User
from app.services.mailer import send_rendered, utc_stamp

log = logging.getLogger("papertick.audit")

#: Rows older than this are pruned by the daily beat; long enough to
#: investigate something noticed late, short enough not to be a dossier.
RETENTION_DAYS = 400


def device_label(request: Request | None) -> str:
    """A human-recognisable name for a browser, from the User-Agent.

    Deliberately coarse — browser and platform family, nothing more. This
    exists so a person can tell one row from another, not to fingerprint them.
    """
    ua = (request.headers.get("user-agent") or "")[:400] if request else ""
    if not ua:
        return "Unknown device"
    browser = next((b for b in ("Edg", "OPR", "Chrome", "Firefox", "Safari") if b in ua), None)
    browser = {"Edg": "Edge", "OPR": "Opera"}.get(browser, browser) or "Browser"
    platform = next(
        (p for p in ("Windows", "Macintosh", "iPhone", "iPad", "Android", "Linux") if p in ua),
        "Unknown device",
    )
    return f"{browser} on {'macOS' if platform == 'Macintosh' else platform}"[:120]


def describe(request: Request | None) -> tuple[str, str]:
    """(ip, device) for a request, both safe to store and to show."""
    from app.rate_limit import client_ip

    if request is None:
        return "unknown", "Unknown device"
    return client_ip(request)[:45], device_label(request)


def record(db: Session, user: User, kind: SecurityEventKind,
           request: Request | None = None, detail: str | None = None) -> SecurityEvent:
    """Write one security-log row. Caller commits."""
    ip, device = describe(request)
    event = SecurityEvent(user_id=user.id, kind=kind, ip=ip, device=device,
                          detail=(detail or None) and detail[:300])
    db.add(event)
    log.info("security event %s for %s from %s", kind.value, user.id, ip)
    return event


def recent(db: Session, user: User, limit: int = 50) -> list[SecurityEvent]:
    return list(db.execute(
        select(SecurityEvent)
        .where(SecurityEvent.user_id == user.id)
        .order_by(SecurityEvent.created_at.desc())
        .limit(limit)
    ).scalars())


def prune(db: Session) -> int:
    """Drop rows past the retention window. Caller commits."""
    from datetime import timedelta

    from app.models import utcnow

    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    n = db.query(SecurityEvent).filter(SecurityEvent.created_at < cutoff).delete(
        synchronize_session=False)
    return n


# ------------------------------------------------------------------ notices

#: What each event is called, and what it says, in the notice email. The
#: wording is the point: a reader has to be able to tell in one line whether
#: the thing that happened is the thing they just did.
NOTICES: dict[SecurityEventKind, tuple[str, str, str]] = {
    SecurityEventKind.PASSWORD_CHANGED: (
        "Your password was changed",
        "Password changed",
        "The sign-in password on your account was just changed. Every other signed-in "
        "session was signed out.",
    ),
    SecurityEventKind.PASSWORD_RESET_COMPLETED: (
        "Your password was reset",
        "Password reset",
        "Your password was reset using an emailed link. Every signed-in session and "
        "every remembered device was cleared.",
    ),
    SecurityEventKind.EMAIL_CHANGE_REQUESTED: (
        "Confirm the change to your sign-in email",
        "Email change requested",
        "Someone asked to change the email address this account signs in with. It does "
        "not change until the link sent to the new address is used.",
    ),
    SecurityEventKind.EMAIL_CHANGED: (
        "Your sign-in email was changed",
        "Email changed",
        "The email address this account signs in with has been changed.",
    ),
    SecurityEventKind.PASSKEY_ADDED: (
        "A passkey was added to your account",
        "Passkey added",
        "A new passkey can sign in to your account on its own. A password change does "
        "not revoke it.",
    ),
    SecurityEventKind.PASSKEY_REMOVED: (
        "A passkey was removed from your account",
        "Passkey removed",
        "A passkey that could sign in to your account has been deleted.",
    ),
    SecurityEventKind.PASSWORDLESS_ENABLED: (
        "Password sign-in was turned off",
        "Passwordless enabled",
        "Your account now signs in with passkeys only. The password path is refused.",
    ),
    SecurityEventKind.PASSWORDLESS_DISABLED: (
        "Password sign-in was turned back on",
        "Passwordless disabled",
        "Your account can sign in with its password again.",
    ),
    SecurityEventKind.MFA_ENABLED: (
        "Two-factor authentication was turned on",
        "Authenticator enabled",
        "Sign-in now asks for a code from your authenticator app.",
    ),
    SecurityEventKind.MFA_DISABLED: (
        "Two-factor authentication was turned off",
        "Authenticator disabled",
        "Sign-in no longer asks for a code from your authenticator app.",
    ),
    SecurityEventKind.LOCKOUT: (
        "Your account was temporarily locked",
        "Sign-in locked",
        "Too many failed sign-in attempts, so sign-in from that source is blocked for a "
        "while. If this wasn't you, someone is guessing your password.",
    ),
    SecurityEventKind.DEVICES_REVOKED: (
        "Remembered devices were cleared",
        "Devices cleared",
        "Every browser that could skip the emailed sign-in code has been forgotten.",
    ),
    SecurityEventKind.API_KEY_CREATED: (
        "An API key was created",
        "API key created",
        "A new API key can read — and, with the trade scope, act on — your account "
        "without signing in.",
    ),
    SecurityEventKind.API_KEY_REVOKED: (
        "An API key was revoked",
        "API key revoked",
        "An API key for your account has been revoked and will no longer work.",
    ),
}

#: Events worth waking someone up for get the alert treatment.
ALERTING = {
    SecurityEventKind.LOCKOUT,
    SecurityEventKind.PASSWORD_RESET_COMPLETED,
    SecurityEventKind.PASSKEY_ADDED,
    SecurityEventKind.PASSWORDLESS_ENABLED,
    SecurityEventKind.MFA_DISABLED,
    SecurityEventKind.EMAIL_CHANGED,
}


def notify(db: Session, user: User, kind: SecurityEventKind,
           request: Request | None = None, detail: str | None = None,
           extra_rows: list[tuple[str, str]] | None = None,
           to: str | None = None) -> SecurityEvent:
    """Record the event and email the user about it.

    `extra_rows` carry the specifics — for an email change, both the old and
    the new address, so the reader can see exactly what is being changed
    rather than being told only that *something* was.
    """
    event = record(db, user, kind, request, detail)
    notice = NOTICES.get(kind)
    if notice is None:
        return event
    subject, heading, lede = notice
    rows = list(extra_rows or [])
    rows += [("When", utc_stamp(event.created_at if event.created_at else None)),
             ("IP address", event.ip),
             ("Device", event.device or "Unknown device")]
    send_rendered(
        to or user.email, f"{subject} — PaperTick",
        heading=heading,
        lede=lede,
        rows=rows,
        alert=kind in ALERTING,
        closing=(
            "If this was you, nothing more is needed. If it wasn't, reset your password "
            f"now at {get_settings().app_url}/forgot-password and review your account's "
            "security activity in Settings."
        ),
    )
    return event
