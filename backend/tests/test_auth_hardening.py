"""Production sign-in, passkey prompting, and password recovery."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request, Response

from app import security
from app.models import (
    PasswordResetToken,
    RefreshToken,
    SecurityEventKind,
    TrustedDevice,
    WebAuthnCredential,
    utcnow,
)
from app.schemas import ForgotPasswordIn, LoginIn, ResetPasswordIn
from app.services import devices, password_reset


def _req(ip: str = "203.0.113.9", cookies: dict | None = None) -> Request:
    headers = [(b"user-agent", b"Mozilla/5.0 (iPhone) Safari/605.1")]
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", jar.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers,
                    "query_string": b"", "client": (ip, 12345)})


@pytest.fixture()
def production(monkeypatch):
    """Production settings, everywhere the sign-in path reads them."""
    from app.config import get_settings

    s = get_settings().model_copy(update={"env": "production", "smtp_host": ""})
    for target in ("app.routers.auth.get_settings",
                   "app.services.devices.get_settings",
                   "app.services.mailer.get_settings",
                   "app.services.audit.get_settings"):
        monkeypatch.setattr(target, lambda: s)
    return s


# ------------------------------------------------- a passkey is not a password

def test_password_login_on_an_unknown_device_needs_a_code_even_with_a_passkey(
    db, user, production, monkeypatch
):
    """The bug: an enrolled passkey suppressed the new-device code on a
    sign-in that used only the password."""
    from app.routers.auth import login

    user.password_hash = security.hash_password("a-strong-pass-123")
    user.email_verified = True
    db.add(WebAuthnCredential(user_id=user.id, credential_id="c1",
                              public_key="pk1", nickname="Bitwarden"))
    db.commit()

    sent: list[str] = []
    monkeypatch.setattr(devices, "start_challenge",
                        lambda db_, u, r: sent.append(u.email) or "challenge-id")

    out = login(LoginIn(email=user.email, password="a-strong-pass-123"),
                _req(), Response(), db)

    assert out.device_verification_required is True
    assert out.tokens is None
    assert sent == [user.email], "the account should have been emailed a code"


def test_a_trusted_browser_still_skips_the_code(db, user, production):
    from app.routers.auth import login

    user.password_hash = security.hash_password("a-strong-pass-123")
    user.email_verified = True
    raw = "known-browser-token"
    db.add(TrustedDevice(user_id=user.id, token_hash=devices._hash(raw),
                         label="Safari on iPhone",
                         expires_at=utcnow() + timedelta(days=10)))
    db.commit()

    out = login(LoginIn(email=user.email, password="a-strong-pass-123"),
                _req(cookies={devices.DEVICE_COOKIE: raw}), Response(), db)
    assert out.tokens is not None
    assert out.device_verification_required is False


def test_verification_required_ignores_enrolled_credentials(db, user, production):
    db.add(WebAuthnCredential(user_id=user.id, credential_id="c2",
                              public_key="pk2", nickname="Key"))
    user.mfa_enabled = True
    db.commit()
    assert devices.has_passkey(db, user) is True
    # neither an enrolled passkey nor enrolled TOTP excuses the password path
    assert devices.verification_required(db, user, _req()) is True


# ------------------------------------------------------------ passkey prompting

def test_both_ceremonies_hint_at_a_credential_manager(db, user, monkeypatch):
    """Without these, iOS leads with "Security Key" and never offers the
    user's password manager."""
    from app.services import passkeys

    monkeypatch.setattr(passkeys, "_store_challenge", lambda k, c: None)
    reg = passkeys.registration_options(db, user)
    _, auth = passkeys.authentication_options()

    assert reg["hints"] == ["client-device", "hybrid"]
    assert auth["hints"] == ["client-device", "hybrid"]
    assert "security-key" not in reg["hints"]


# ------------------------------------------------------------- password recovery

def test_forgot_password_says_the_same_thing_for_unknown_addresses(db, user, production):
    from app.routers.auth import forgot_password

    known = forgot_password(ForgotPasswordIn(email=user.email), _req(), db)
    unknown = forgot_password(ForgotPasswordIn(email="nobody@example.com"), _req(), db)
    assert known == unknown == {"status": "sent_if_registered"}
    # ...but only the real one minted anything
    assert db.query(PasswordResetToken).count() == 1


def test_a_reset_link_works_once_and_clears_every_session(db, user, production):
    from app.routers.auth import reset_password

    user.password_hash = security.hash_password("a-strong-pass-123")
    db.add(RefreshToken(user_id=user.id, token_hash="r" * 64,
                        expires_at=utcnow() + timedelta(days=7)))
    db.add(TrustedDevice(user_id=user.id, token_hash="d" * 64, label="Old browser",
                         expires_at=utcnow() + timedelta(days=10)))
    db.commit()

    raw = password_reset.issue(db, user, "203.0.113.9")
    db.commit()

    out = reset_password(ResetPasswordIn(token=raw, new_password="a-brand-new-pass-9"),
                         _req(), db)
    assert out["status"] == "reset"
    assert security.verify_password("a-brand-new-pass-9", user.password_hash)

    # every session and every remembered browser is gone
    assert db.query(RefreshToken).filter(RefreshToken.revoked_at.is_(None)).count() == 0
    assert db.query(TrustedDevice).filter(TrustedDevice.revoked_at.is_(None)).count() == 0

    # and the link cannot be replayed
    with pytest.raises(HTTPException) as exc:
        reset_password(ResetPasswordIn(token=raw, new_password="another-pass-1234"), _req(), db)
    assert exc.value.status_code == 400


def test_a_second_request_invalidates_the_first_link(db, user, production):
    from app.routers.auth import reset_password

    first = password_reset.issue(db, user, "203.0.113.9")
    db.commit()
    second = password_reset.issue(db, user, "203.0.113.9")
    db.commit()

    with pytest.raises(HTTPException):
        reset_password(ResetPasswordIn(token=first, new_password="a-brand-new-pass-9"),
                       _req(), db)
    assert reset_password(
        ResetPasswordIn(token=second, new_password="a-brand-new-pass-9"), _req(), db
    )["status"] == "reset"


def test_an_expired_link_is_refused(db, user, production):
    from app.routers.auth import reset_password

    raw = password_reset.issue(db, user, "203.0.113.9")
    row = db.query(PasswordResetToken).one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        reset_password(ResetPasswordIn(token=raw, new_password="a-brand-new-pass-9"),
                       _req(), db)
    assert exc.value.status_code == 400


def test_signing_in_cancels_an_outstanding_reset_link(db, user, production):
    """"If this wasn't you, ignore it" has to actually be true."""
    from app.routers.auth import login, reset_password

    user.password_hash = security.hash_password("a-strong-pass-123")
    user.email_verified = True
    db.add(TrustedDevice(user_id=user.id, token_hash=devices._hash("trusted"),
                         label="Safari on iPhone",
                         expires_at=utcnow() + timedelta(days=10)))
    db.commit()

    raw = password_reset.issue(db, user, "198.51.100.7")
    db.commit()

    login(LoginIn(email=user.email, password="a-strong-pass-123"),
          _req(cookies={devices.DEVICE_COOKIE: "trusted"}), Response(), db)

    with pytest.raises(HTTPException):
        reset_password(ResetPasswordIn(token=raw, new_password="a-brand-new-pass-9"),
                       _req(), db)


def test_a_weak_new_password_is_refused_before_the_token_is_spent(db, user, production):
    from app.routers.auth import reset_password

    raw = password_reset.issue(db, user, "203.0.113.9")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        reset_password(ResetPasswordIn(token=raw, new_password="short"), _req(), db)
    assert exc.value.status_code == 422
    # the link survives a rejected attempt, so a typo does not cost a round trip
    assert db.query(PasswordResetToken).filter(
        PasswordResetToken.used_at.is_(None)).count() == 1


# ------------------------------------------------------------------ the IP trail

def test_the_security_log_keeps_the_originating_address(db, user, production):
    from app.routers.auth import login, security_activity
    from app.deps import Principal

    user.password_hash = security.hash_password("a-strong-pass-123")
    user.email_verified = True
    db.add(TrustedDevice(user_id=user.id, token_hash=devices._hash("t"),
                         label="Safari on iPhone",
                         expires_at=utcnow() + timedelta(days=10)))
    db.commit()

    login(LoginIn(email=user.email, password="a-strong-pass-123"),
          _req(ip="198.51.100.42", cookies={devices.DEVICE_COOKIE: "t"}), Response(), db)

    rows = security_activity(50, Principal(user=user, scopes={"manage"}), db)
    signin = next(r for r in rows if r.kind == SecurityEventKind.SIGN_IN.value)
    assert signin.ip == "198.51.100.42"
    assert signin.device == "Safari on iPhone"
